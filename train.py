import torch
import torch.nn.functional as F
import torch.optim as optim
from model import Model
from video_dataset import Dataset
from tensorboard_logger import log_value
import utils
import numpy as np
from torch.autograd import Variable
import time
torch.set_default_tensor_type('torch.cuda.FloatTensor')


def CLSLOSS(logits, seq_len, batch_size, labels, device):
    ''' logits: torch tensor of dimension (B, T, 20),
        seq_len: numpy array of dimension (B,) indicating the length of each video in the batch, 
        labels: torch tensor of dimension (B, 20) of 1 or 0
        return: torch tensor of dimension 0 (value) '''

    k = np.ceil(seq_len/8).astype('int32')
    labels = labels / (torch.sum(labels, dim=1, keepdim=True) + 1e-10)  # 注意这一步对label取均值的操作
    lab = torch.zeros(0).to(device)
    instance_logits = torch.zeros(0).to(device)
    for i in range(batch_size):
        if seq_len[i] < 5 or labels[i].sum() == 0:
            continue
        tmp, _ = torch.topk(logits[i][:seq_len[i]], k=int(k[i]), dim=0)
        instance_logits = torch.cat([instance_logits, torch.mean(tmp, 0, keepdim=True)], dim=0)
        lab = torch.cat([lab, labels[[i]]], dim=0)
    clsloss = -torch.mean(torch.sum(Variable(lab) * F.log_softmax(instance_logits, dim=1), dim=1), dim=0)
    return clsloss


def COUNTINGLOSS(features, gt_count, seq_len, device):
    ''' features: torch tensor dimension  (B, T, 20),
        gt_count: torch tensor dimension  (B, 20) of integer value,
        seq_len: numpy array of dimension (B,) indicating the length of each video in the batch, 
        return: torch tensor of dimension 0 (value) '''

    pos_loss, neg_loss, num = 0, 0, 0
    inv_gt_count = (gt_count > 0).float() / (gt_count + 1e-10)  # count愈大，权重越小
    for i in range(features.size(0)):  # B
        # categories present in video
        mask_pos = (gt_count[i]<int(seq_len[i])) * (gt_count[i]>0)
        # categories absent
        mask_neg = (mask_pos==0)
        pred_count = (features[i,:seq_len[i]]).sum(0)  # [0.1147,0.3137,....-0.2102]  每个类别的预测count数
        # 相对误差
        pos_loss += ((pred_count[mask_pos] - Variable(gt_count[i][mask_pos],requires_grad=False)) * inv_gt_count[i][mask_pos]).abs().sum() # relative L1
        # 绝对误差
        neg_loss += 0.001* pred_count[mask_neg==1].abs().sum()
        num += 1
    if num > 0:
        return (pos_loss+neg_loss)/num
    else:
        return torch.zeros(1).to(device)

#attention机制可以认为是扩大不同时间维度特征的差异
def CENTERLOSS(features, logits, labels, seq_len, criterion, itr, device):
    ''' features: torch tensor dimension  (B, T, 1024),
        logits: torch tensor of dimension (B, T, 20),
        labels: torch tensor of dimension (B, 20) of 1 or 0,
        seq_len: numpy array of dimension (B,) indicating the length of each video in the batch, 
        criterion: center loss criterion, 
        return: torch tensor of dimension 0 (value) '''

    lab = torch.zeros(0).to(device)  # [15]  视频动作的类别索引
    feat = torch.zeros(0).to(device) # [(1,1024),(1,1024),...(1,1024)]-->(B,1024)  加权后的视频特征包含了所有时序的信息
    itr_th = 5000 
    for i in range(features.size(0)):
        if (labels[i] > 0).sum() == 0 or ((labels[i] > 0).sum() != 1 and itr < itr_th):  # 前5000次迭代只计算类别数为1的视频
            continue
        # categories present in the video
        labi = torch.arange(labels.size(1))[labels[i]>0]  # [7]  # 类别的索引
        # 沿着时间轴计算attention
        atn = F.softmax(logits[i][:seq_len[i]], dim=0)  # (T,20) 
        atni = atn[:,labi]  # (T,1)  挑选该类的attention
        # aggregate features category-wise
        for l in range(len(labi)):
            labl = labi[[l]].float()  # 有效的类别索引
            atnl = atni[:,[l]]  # (T,1) 只选取与该类对应的attention
            atnl[atnl<atnl.mean()] = 0  # (T,1)  将低于阈值的地方置为0，从视频的特定类别的高关注区域进行特征聚合。
            sum_atn = atnl.sum()
            if sum_atn > 0:
                atnl = atnl.expand(seq_len[i],features.size(2))  # (T,1024)
                # attention-weighted feature aggregation
                featl = torch.sum(features[i][:seq_len[i]]*atnl,dim=0,keepdim=True)/sum_atn # (1,1024)经过attention聚合后的特征，包含了所有时序位置的信息
                feat = torch.cat([feat, featl], dim=0)   # 参与计算centerLoss的特征 
                lab = torch.cat([lab, labl], dim=0)      # 类别
        
    if feat.numel() > 0:  # feat: (B,1024) lab: [15]
        # Compute loss
        loss = criterion(feat, lab)  # (B,1024), (B) loss:4293.83
        return loss / feat.size(0)
    else:
        return 0


def train(itr, dataset, args, model, optimizer, criterion_cent_all, optimizer_centloss_all, logger, device):

    criterion_cent_f = criterion_cent_all[0]
    criterion_cent_r = criterion_cent_all[1]
    optimizer_centloss_f = optimizer_centloss_all[0] 
    optimizer_centloss_r = optimizer_centloss_all[1] 
    countloss_mult = 0.1 if args.activity_net else 1
    countloss, centerloss_alpha = torch.zeros(1), 0.001  # center loss计算出来会非常大因此乘以0.001
    centloss_itr, count_itr = 0, 0

    # Batch fprop
    features, labels, count_labels = dataset.load_data()   # (2,750,2048), (2,20), (2,20)
    seq_len = np.sum(np.max(np.abs(features), axis=2) > 0, axis=1) # [78,194] 有效的特征长度
    features = features[:,:np.max(seq_len),:]  # (2,194,2048)

    features = torch.from_numpy(features).float().to(device)
    labels = torch.from_numpy(labels).float().to(device)
    count_labels = torch.from_numpy(count_labels).float().to(device)
    
    # (B,T,1024),(B,T,20),(B,T,1024),(B,T,20),(B,T,20),(B,T,20) tcam为双流融合后的分类得分，count_feat为attention加权后的得分
    features_f, logits_f, features_r, logits_r, tcam, count_feat = model(Variable(features), device, seq_len=torch.from_numpy(seq_len).to(device))
    
    # Classification loss for two streams and final tcam
    clsloss_f = CLSLOSS(logits_f, seq_len, args.batch_size, labels, device)  # logits_f : (B,T,20)  labels: (B,20)
    clsloss_r = CLSLOSS(logits_r, seq_len, args.batch_size, labels, device)  # logits_r : (B,T,20)  labels: (B,20)  
    clsloss_final = CLSLOSS(tcam, seq_len, args.batch_size, labels, device)  # tcam : (B,T,20)  labels: (B,20)
    clsloss = clsloss_f + clsloss_r + clsloss_final
    total_loss = clsloss

    # Add center loss of both streams
    if itr > centloss_itr:
        centloss_f = CENTERLOSS(features_f, logits_f, labels, seq_len, criterion_cent_f, itr, device) * centerloss_alpha # 0.001
        optimizer_centloss_f.zero_grad()
        centloss_r = CENTERLOSS(features_r, logits_r, labels, seq_len, criterion_cent_r, itr, device) * centerloss_alpha
        optimizer_centloss_r.zero_grad()
        centloss = centloss_f + centloss_r
        total_loss += centloss

    # Add counting loss every alternate batch
    if (itr % 2 == 0) and itr > count_itr:   # 每迭代两次计算一次countloss
        countloss = COUNTINGLOSS(count_feat, count_labels, seq_len, device) * countloss_mult # 0.1  count_feat为注意力加权后的分类结果
        if countloss.item() > 0:
            total_loss += countloss 

    logger.log_value('total_loss', total_loss, itr)
    print('Iteration: %d, Loss: %.3f' %(itr, total_loss.data.cpu().numpy()))

    # 注意了模型的参数和center_loss的参数训练是不一样的
    optimizer.zero_grad()
    if total_loss > 0:
        total_loss.backward()
        
        
    # Update centers
    if itr > centloss_itr:
        for param in criterion_cent_f.parameters():
            if param.grad is not None:
                param.grad.data *= (1./centerloss_alpha)  # 0.001
        optimizer_centloss_f.step()
        for param in criterion_cent_r.parameters():
            if param.grad is not None:
                param.grad.data *= (1./centerloss_alpha)
        optimizer_centloss_r.step()
    # Update model params
    if total_loss > 0:
        optimizer.step()
        
        


