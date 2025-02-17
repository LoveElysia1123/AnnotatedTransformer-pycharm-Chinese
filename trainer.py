import os
from os.path import exists
import torch
import torch.nn as nn
from torch.nn.functional import log_softmax, pad
import math
import copy
import time
from torch.optim.lr_scheduler import LambdaLR
import pandas as pd
import altair as alt
from torchtext.data.functional import to_map_style_dataset
from torch.utils.data import DataLoader
from torchtext.vocab import build_vocab_from_iterator
import torchtext.datasets as datasets
import spacy
import GPUtil
import warnings
from torch.utils.data.distributed import DistributedSampler
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel as DDP

warnings.filterwarnings("ignore")

from utils import subsequent_mask


class Batch:
    """Object for holding a batch of data with mask during training."""

    def __init__(self, src, tgt=None, pad=2):  # 2 = <blank>
        self.src = src
        self.src_mask = (src != pad).unsqueeze(-2)  # 只掩盖填充标记, [batch_size, 1, src_len]
        if tgt is not None:
            self.tgt = tgt[:, :-1]  # 使用目标句子构建数据集
            self.tgt_y = tgt[:, 1:]  # 模型的功能是预测下一个
            # 掩盖填充标记和下一个单词, [batch_size, tgt_len, tgt_len]
            self.tgt_mask = self.make_std_mask(self.tgt, pad)
            self.ntokens = (self.tgt_y != pad).data.sum()  # 计算目标数据中非填充值的数量

    @staticmethod
    def make_std_mask(tgt, pad):
        """
        假设tgt是两个句子，tgt的维度是[2,4]，也就是句子长度是4，那么处理后的tgt_mask是[2,1,4]，
        而subsequent_mask是[1,4,4],进行按位与操作会对张量进行广播，也就是最终的维度会变成[2, 4, 4]
        """
        tgt_mask = (tgt != pad).unsqueeze(-2)
        tgt_mask = tgt_mask & subsequent_mask(tgt.size(-1)).type_as(
            tgt_mask.data
        )
        return tgt_mask


class SimpleLossCompute:
    """A simple loss compute and train function."""

    def __init__(self, generator, criterion):
        self.generator = generator
        self.criterion = criterion

    def __call__(self, x, y, norm):
        x = self.generator(x)
        sloss = (
                self.criterion(
                    x.contiguous().view(-1, x.size(-1)), y.contiguous().view(-1)
                )
                / norm
        )
        return sloss.data * norm, sloss


class LabelSmoothing(nn.Module):
    "Implement label smoothing."

    def __init__(self, size, padding_idx, smoothing=0.0):
        super(LabelSmoothing, self).__init__()
        self.criterion = nn.KLDivLoss(reduction="sum")
        self.padding_idx = padding_idx
        self.confidence = 1.0 - smoothing
        self.smoothing = smoothing
        self.size = size
        self.true_dist = None

    def forward(self, x, target):
        assert x.size(1) == self.size
        true_dist = x.data.clone()
        true_dist.fill_(self.smoothing / (self.size - 2))  # 将所有值填充为平滑概率（去除填充标签和正确标签）
        # dim=1, index=target.data.unsqueeze(1), src=self.confidence
        # 在正确的标签位置分配置信标签概率
        true_dist.scatter_(1, target.data.unsqueeze(1), self.confidence)
        true_dist[:, self.padding_idx] = 0  # 填充标志位置的概率依然是0
        # 如果目标预测单词中存在填充标记，则将其对应位置的概率设置为0，也就是模型不预测填充标记
        mask = torch.nonzero(target.data == self.padding_idx)
        if mask.dim() > 0:
            true_dist.index_fill_(0, mask.squeeze(), 0.0)
        self.true_dist = true_dist
        return self.criterion(x, true_dist.clone().detach())


class TrainState:
    """
    Track number of steps, examples, and tokens processed
    """

    step: int = 0  # Steps in the current epoch
    accum_step: int = 0  # Number of gradient accumulation steps
    samples: int = 0  # total # of examples used
    tokens: int = 0  # total # of tokens processed


def run_epoch(
        data_iter,
        model,
        loss_compute,
        optimizer,
        scheduler,
        mode="train",
        accum_iter=1,
        train_state=TrainState(),
):
    """
    Train a single epoch
    :param data_iter: 用于进行训练的数据迭代器
    :param model: 训练所用的transformer模型
    :param loss_compute: 计算loss的类，继承自nn.Module，调用时是进行前向传播计算loss
    :param optimizer: 进行反向传播的优化器
    :param scheduler: 学习率调整策略
    :param mode: 训练模式，使用train时会进行反向传播
    :param accum_iter: 迭代一定次数后进行反向传播和梯度清空
    :param train_state: 训练状态记录器
    :return: 训练过程中所有Token的平均损失值和训练状态记录器
    """
    start = time.time()
    total_tokens = 0  # 记录已经用于训练的Token个数
    total_loss = 0  # 训练过程中损失值之和
    tokens = 0  # 记录已经用于训练的Token个数，用于输出信息时计算每秒的Token处理数
    n_accum = 0  # 记录参数更新的次数
    for i, batch in enumerate(data_iter):
        # 对输入的模型进行一次前向传播
        out = model.forward(
            batch.src, batch.tgt, batch.src_mask, batch.tgt_mask
        )
        loss, loss_node = loss_compute(out, batch.tgt_y, batch.ntokens)
        # loss_node = loss_node / accum_iter
        if mode == "train" or mode == "train+log":
            loss_node.backward()
            train_state.step += 1
            train_state.samples += batch.src.shape[0]
            train_state.tokens += batch.ntokens
            if i % accum_iter == 0:
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
                n_accum += 1
                train_state.accum_step += 1
            scheduler.step()

        total_loss += loss
        total_tokens += batch.ntokens
        tokens += batch.ntokens
        if i % 40 == 1 and (mode == "train" or mode == "train+log"):
            lr = optimizer.param_groups[0]["lr"]  # 在字典中取出当前学习率
            elapsed = time.time() - start
            print(
                (
                        "Epoch Step: %6d | Accumulation Step: %3d | Loss: %6.2f "
                        + "| Tokens / Sec: %7.1f | Learning Rate: %6.1e"
                )
                % (i, n_accum, loss / batch.ntokens, tokens / elapsed, lr)
            )
            start = time.time()
            tokens = 0
        del loss
        del loss_node
    return total_loss / total_tokens, train_state
