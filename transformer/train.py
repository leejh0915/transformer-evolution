import sys
sys.path.append("..")
import os, argparse, datetime, time, re, collections, random
from tqdm import tqdm, trange
import numpy as np
import wandb

import torch
import torch.distributed as dist
import torch.nn as nn
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel

from vocab import load_vocab
import config as cfg
import model as transformer
import data
import optimization as optim

""" random seed """
def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


""" init_process_group """ 
def init_process_group(rank, world_size):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12355'
    dist.init_process_group("gloo", rank=rank, world_size=world_size)


""" destroy_process_group """
def destroy_process_group():
    dist.destroy_process_group()


""" 모델 epoch 평가 """
def eval_epoch(config, rank, model, data_loader):
    matchs = []
    model.eval()

    n_word_total = 0
    n_correct_total = 0
    with tqdm(total=len(data_loader), desc=f"Valid({rank})") as pbar:
        for i, value in enumerate(data_loader):
            labels, enc_inputs, dec_inputs = map(lambda v: v.to(config.device), value)

            outputs = model(enc_inputs, dec_inputs)
            logits = outputs[0]
            _, indices = logits.max(1)

            match = torch.eq(indices, labels).detach()
            matchs.extend(match.cpu())
            accuracy = np.sum(matchs) / len(matchs) if 0 < len(matchs) else 0

            pbar.update(1)
            pbar.set_postfix_str(f"Acc: {accuracy:.3f}")
    return np.sum(matchs) / len(matchs) if 0 < len(matchs) else 0


""" 모델 epoch 학습 """
def train_epoch(config, rank, epoch, model, criterion, optimizer, scheduler, train_loader):
    losses = []
    model.train()

    with tqdm(total=len(train_loader), desc=f"Train({rank}) {epoch}") as pbar:
        for i, value in enumerate(train_loader):
            labels, enc_inputs, dec_inputs = map(lambda v: v.to(config.device), value)

            optimizer.zero_grad()
            outputs = model(enc_inputs, dec_inputs)
            logits = outputs[0]

            loss = criterion(logits, labels)
            loss_val = loss.item()
            losses.append(loss_val)

            loss.backward()
            optimizer.step()
            scheduler.step()

            pbar.update(1)
            pbar.set_postfix_str(f"Loss: {loss_val:.3f} ({np.mean(losses):.3f})")
    return np.mean(losses)


""" 모델 학습 """
def train_model(rank, world_size, args):
    if 1 < args.n_gpu:
        init_process_group(rank, world_size)
    master = (world_size == 0 or rank % world_size == 0)

    if master:
        wandb.init(project='transformer-evolution')

    vocab = load_vocab(args.vocab)

    config = cfg.Config.load(args.config)
    config.n_enc_vocab, config.n_dec_vocab = len(vocab), len(vocab)
    config.device = torch.device(f"cuda:{rank}" if torch.cuda.is_available() else "cpu")
    print(config)

    best_epoch, best_loss, best_score = 0, 0, 0
    model = transformer.MovieClassification(config)
    if os.path.isfile(args.save):
        best_epoch, best_loss, best_score = model.load(args.save)
        print(f"rank: {rank} load state dict from: {args.save}")
    if 1 < args.n_gpu:
        model.to(config.device)
        model = DistributedDataParallel(model, device_ids=[rank], find_unused_parameters=True)
    else:
        model.to(config.device)
    if master: wandb.watch(model)

    criterion = torch.nn.CrossEntropyLoss()

    train_loader, train_sampler = data.build_data_loader(vocab, "../data/ratings_train.json", args, shuffle=True)
    test_loader, _ = data.build_data_loader(vocab, "../data/ratings_test.json", args, shuffle=False)

    t_total = len(train_loader) * args.epoch
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': args.weight_decay},
        {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
    optimizer = optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = optim.get_linear_schedule_with_warmup(optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=t_total)

    offset = best_epoch
    for step in trange(args.epoch, desc="Epoch"):
        if train_sampler:
            train_sampler.set_epoch(step)
        epoch = step + offset

        loss = train_epoch(config, rank, epoch, model, criterion, optimizer, scheduler, train_loader)
        score = eval_epoch(config, rank, model, test_loader)
        if master: wandb.log({"loss": loss, "accuracy": score})

        if master and best_score < score:
            best_epoch, best_loss, best_score = epoch, loss, score
            if isinstance(model, DistributedDataParallel):
                model.module.save(best_epoch, best_loss, best_score, args.save)
            else:
                model.save(best_epoch, best_loss, best_score, args.save)
            print(f">>>> rank: {rank} save model to {args.save}, epoch={best_epoch}, loss={best_loss:.3f}, socre={best_score:.3f}")

    if 1 < args.n_gpu:
        destroy_process_group()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config_half.json", type=str, required=False,
                        help="config file")
    parser.add_argument("--vocab", default="../kowiki.model", type=str, required=False,
                        help="vocab file")
    parser.add_argument("--save", default="save_best.pth", type=str, required=False,
                        help="save file")
    # parser.add_argument("--epoch", default=20, type=int, required=False,
    #                     help="epoch")
    parser.add_argument("--epoch", default=2, type=int, required=False,
                        help="epoch")
    parser.add_argument("--batch", default=512, type=int, required=False,
                        help="batch")
    parser.add_argument("--gpu", default=None, type=int, required=False,
                        help="GPU id to use.")
    parser.add_argument('--seed', type=int, default=42, required=False,
                        help="random seed for initialization")
    parser.add_argument('--weight_decay', type=float, default=0, required=False,
                        help="weight decay")
    parser.add_argument('--learning_rate', type=float, default=5e-5, required=False,
                        help="learning rate")
    parser.add_argument('--adam_epsilon', type=float, default=1e-8, required=False,
                        help="adam epsilon")
    parser.add_argument('--warmup_steps', type=float, default=0, required=False,
                        help="warmup steps")

    args = parser.parse_args()

    if torch.cuda.is_available():
        args.n_gpu = torch.cuda.device_count() if args.gpu is None else 1
    else:
        args.n_gpu = 0
    set_seed(args)

    if 1 < args.n_gpu:
        mp.spawn(train_model,
             args=(args.n_gpu, args),
             nprocs=args.n_gpu,
             join=True)
    else:
        train_model(0 if args.gpu is None else args.gpu, args.n_gpu, args)

        # os.environ['CUDA_LAUNCH_BLOCKING'] = "1"
        # os.environ["CUDA_VISIBLE_DEVICES"] = "0"
