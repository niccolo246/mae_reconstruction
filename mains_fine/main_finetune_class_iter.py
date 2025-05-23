# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# DeiT: https://github.com/facebookresearch/deit
# BEiT: https://github.com/microsoft/unilm/tree/master/beit
# --------------------------------------------------------

# use this for fine-grained saving of model-parameter weights .
# will save validate depending on provided validate_every parameter as opposed to every epoch as in original

import argparse
import datetime
import json
import numpy as np
import os
import time
from pathlib import Path


import torch
import torch.backends.cudnn as cudnn
from torch.utils.tensorboard import SummaryWriter

import timm

#assert timm.__version__ == "0.3.2"  # version check
from timm.models.layers import trunc_normal_
from timm.data.mixup import Mixup
from timm.loss import LabelSmoothingCrossEntropy, SoftTargetCrossEntropy

import util.lr_decay as lrd
import util.misc as misc

from util.pos_embed import interpolate_pos_embed
from util.misc import NativeScalerWithGradNormCount as NativeScaler

import models_vit

from engine_finetune import train_one_epoch_iter, evaluate

from datasets_three_d_fine import Custom3DDataset

from monai.transforms import (
    Compose, RandGaussianNoise, RandGaussianSmooth, RandAdjustContrast,
)


def get_args_parser():
    parser = argparse.ArgumentParser('MAE fine-tuning for image classification', add_help=False)
    parser.add_argument('--batch_size', default=3, type=int,
                        help='Batch size per GPU (effective batch size is batch_size * accum_iter * # gpus')
    parser.add_argument('--epochs', default=200, type=int)
    parser.add_argument('--accum_iter', default=4, type=int,
                        help='Accumulate gradient iterations (for increasing the effective batch size under memory constraints)')

    # Model parameters
    parser.add_argument('--model', default='vit_large_patch16_yo', type=str, metavar='MODEL',
                        help='Name of model to train')

    parser.add_argument('--input_size', default=256, type=int,
                        help='images input size')

    parser.add_argument('--drop_path', type=float, default=0, metavar='PCT',
                        help='Drop path rate (default: 0.1)')

    # Optimizer parameters
    parser.add_argument('--clip_grad', type=float, default=None, metavar='NORM',
                        help='Clip gradient norm (default: None, no clipping)')
    parser.add_argument('--weight_decay', type=float, default=0.05,
                        help='weight decay (default: 0.05)')

    parser.add_argument('--lr', type=float, default=0.0001, metavar='LR',
                        help='learning rate (absolute lr)')
    parser.add_argument('--blr', type=float, default=1e-3, metavar='LR',
                        help='base learning rate: absolute_lr = base_lr * total_batch_size / 256')
    parser.add_argument('--layer_decay', type=float, default=0.75,
                        help='layer-wise lr decay from ELECTRA/BEiT')

    parser.add_argument('--min_lr', type=float, default=1e-6, metavar='LR',
                        help='lower lr bound for cyclic schedulers that hit 0')

    parser.add_argument('--warmup_epochs', type=int, default=5, metavar='N',
                        help='epochs to warmup LR')

    parser.add_argument('--smoothing', type=float, default=0.1,
                        help='Label smoothing (default: 0.1)')

    # * Finetuning params
    parser.add_argument('--finetune', default='',
                        help='finetune from checkpoint')
    parser.add_argument('--global_pool', action='store_true')
    parser.set_defaults(global_pool=True)
    parser.add_argument('--cls_token', action='store_false', dest='global_pool',
                        help='Use class token instead of global pool for classification')

    # Dataset parameters
    parser.add_argument('--data_path_tr', default='required', type=str,
                        help='dataset path')
    parser.add_argument('--data_path_val', default='required', type=str,
                        help='dataset path')
    parser.add_argument('--nb_classes', default=1, type=int,
                        help='number of the classification types')

    parser.add_argument('--regression', default=False, type=int,
                        help='flag regression task')

    parser.add_argument('--binary_class', default=True, type=int,
                        help='flag binary classification')

    parser.add_argument('--binary_class_weights', default=None, type=int,  # None . # nodule: [0.67] . # rad-chest: [4.0, 23.6, 0.9, 68.3, 5.2, 7.6]
                        help='flag binary classification')

    parser.add_argument('--output_dir', default='required',
                        help='path where to save, empty for no saving')
    parser.add_argument('--log_dir', default='required',
                        help='path where to tensorboard log')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=5, type=int)
    parser.add_argument('--resume', default='',
                        help='resume from checkpoint')

    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', action='store_true',
                        help='Perform evaluation only')
    parser.add_argument('--dist_eval', action='store_true', default=False,
                        help='Enabling distributed evaluation (recommended during training for faster monitor')
    parser.add_argument('--num_workers', default=1, type=int)
    parser.add_argument('--pin_mem', action='store_true',
                        help='Pin CPU memory in DataLoader for more efficient (sometimes) transfer to GPU.')
    parser.add_argument('--no_pin_mem', action='store_false', dest='pin_mem')
    parser.set_defaults(pin_mem=False)

    # * Mixup params - note deactivated
    parser.add_argument('--mixup', type=float, default=0,
                        help='mixup alpha, mixup enabled if > 0.')
    parser.add_argument('--cutmix', type=float, default=0,
                        help='cutmix alpha, cutmix enabled if > 0.')
    parser.add_argument('--cutmix_minmax', type=float, nargs='+', default=None,
                        help='cutmix min/max ratio, overrides alpha and enables cutmix if set (default: None)')
    parser.add_argument('--mixup_prob', type=float, default=1.0,
                        help='Probability of performing mixup or cutmix when either/both is enabled')
    parser.add_argument('--mixup_switch_prob', type=float, default=0.5,
                        help='Probability of switching to cutmix when both mixup and cutmix enabled')
    parser.add_argument('--mixup_mode', type=str, default='batch',
                        help='How to apply mixup/cutmix params. Per "batch", "pair", or "elem"')

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--local_rank', default=-1, type=int)
    parser.add_argument('--dist_on_itp', action='store_true')
    parser.add_argument('--dist_url', default='env://',
                        help='url used to set up distributed training')

    return parser


def main(args):
    misc.init_distributed_mode(args)

    print('job dir: {}'.format(os.path.dirname(os.path.realpath(__file__))))
    print("{}".format(args).replace(', ', ',\n'))

    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + misc.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)

    cudnn.benchmark = True


    ######## ######## ######## ######## ######## ######## ######## ######## ######## ######## ######## ########
    # Add additional augmentation here

    # Define the transformations
    transform_train = Compose([
        RandGaussianNoise(
            prob=0.1,
            mean=0.0,
            std=0.1
        ),
        RandGaussianSmooth(
            prob=0.2,
            sigma_x=(0.5, 1),
            sigma_y=(0.5, 1),
            sigma_z=(0.5, 1)
        ),
        RandAdjustContrast(
            prob=0.15,
            gamma=(0.75, 1.25)
        ),
    ])

    ######## ######## ######## ######## ######## ######## ######## ######## ######## ######## ######## ########
    # Choose Custom dataloader here
    dataset_train = Custom3DDataset(csv_path=args.data_path_tr, transform=transform_train)
    dataset_val = Custom3DDataset(csv_path=args.data_path_val, transform=None)
    ######## ######## ######## ######## ######## ######## ######## ######## ######## ######## ######## ########

    if True:  # args.distributed:
        num_tasks = misc.get_world_size()
        global_rank = misc.get_rank()
        sampler_train = torch.utils.data.DistributedSampler(
            dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True
        )
        print("Sampler_train = %s" % str(sampler_train))
        if args.dist_eval:
            if len(dataset_val) % num_tasks != 0:
                print('Warning: Enabling distributed evaluation with an eval dataset not divisible by process number. '
                      'This will slightly alter validation results as extra duplicate entries are added to achieve '
                      'equal num of samples per-process.')
            sampler_val = torch.utils.data.DistributedSampler(
                dataset_val, num_replicas=num_tasks, rank=global_rank, shuffle=True)  # shuffle=True to reduce monitor bias
        else:
            sampler_val = torch.utils.data.SequentialSampler(dataset_val)
    else:
        sampler_train = torch.utils.data.RandomSampler(dataset_train)
        sampler_val = torch.utils.data.SequentialSampler(dataset_val)

    if global_rank == 0 and args.log_dir is not None and not args.eval:
        os.makedirs(args.log_dir, exist_ok=True)
        log_writer = SummaryWriter(log_dir=args.log_dir)
    else:
        log_writer = None

    # For single GPU training, we set shuffle=True to shuffle the data.
    # In a distributed setting, a DistributedSampler (which handles shuffling across processes)
    # should be used. Here, we leave the sampler commented out for single GPU use.
    data_loader_train = torch.utils.data.DataLoader(
        dataset_train,
        # When using distributed training, uncomment the sampler below and remove shuffle=True.
        # sampler=sampler_train,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        pin_memory=args.pin_mem,
        drop_last=True,
        shuffle=True  # Use shuffling for single GPU training.
    )

    data_loader_val = torch.utils.data.DataLoader(
        dataset_val, sampler=sampler_val,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        pin_memory=args.pin_mem,
        drop_last=False
    )

    ######## ######## ######## ######## ######## ######## ######## ######## ######## ######## ######## ########
    mixup_fn = None
    mixup_active = args.mixup > 0 or args.cutmix > 0. or args.cutmix_minmax is not None
    if mixup_active:
        print("Mixup is activated!")
        mixup_fn = Mixup(
            mixup_alpha=args.mixup, cutmix_alpha=args.cutmix, cutmix_minmax=args.cutmix_minmax,
            prob=args.mixup_prob, switch_prob=args.mixup_switch_prob, mode=args.mixup_mode,
            label_smoothing=args.smoothing, num_classes=args.nb_classes)

    ######## ######## ######## ######## ######## ######## ######## ######## ######## ######## ######## ########

    model = models_vit.__dict__[args.model](
        num_classes=args.nb_classes,
        drop_path_rate=args.drop_path,
        global_pool=args.global_pool,
    )

    if args.finetune and not args.eval:
        checkpoint = torch.load(args.finetune, map_location='cpu')
        print("Load pre-trained checkpoint from: %s" % args.finetune)
        checkpoint_model = checkpoint['model'] if "model" in checkpoint.keys() else checkpoint['model_state']

        if 'pos_embed' in checkpoint_model:
            interpolate_pos_embed(model, checkpoint_model)

        # Filter out keys that do not match in shape
        model_dict = model.state_dict()
        filtered_checkpoint = {k: v for k, v in checkpoint_model.items() if k in model_dict and model_dict[k].shape == v.shape}

        # Load the filtered checkpoint into the model
        msg = model.load_state_dict(filtered_checkpoint, strict=False)
        print(msg)

        # manually initialize fc layer
        if args.model == "vit_large_patch16_yo":
            trunc_normal_(model.head.weight, std=2e-5)
        else:
            # Initialize all Linear layers in the head
            for module in model.head:
                if isinstance(module, torch.nn.Linear):
                    trunc_normal_(module.weight, std=2e-5)
                    if module.bias is not None:
                        torch.nn.init.zeros_(module.bias)  # Initialize bias to zeros

    model.to(device)

    model_without_ddp = model
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)

    print("Model = %s" % str(model_without_ddp))
    print('number of params (M): %.2f' % (n_parameters / 1.e6))

    eff_batch_size = args.batch_size * args.accum_iter * misc.get_world_size()
    
    if args.lr is None:  # only base_lr is specified
        args.lr = args.blr * eff_batch_size / 256

    print("base lr: %.2e" % (args.lr * 256 / eff_batch_size))
    print("actual lr: %.2e" % args.lr)

    print("accumulate grad iterations: %d" % args.accum_iter)
    print("effective batch size: %d" % eff_batch_size)

    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module

    param_groups = lrd.param_groups_lrd(
            model_without_ddp,
            args.weight_decay,
            no_weight_decay_list=model_without_ddp.no_weight_decay(),
            layer_decay=args.layer_decay
        )
    optimizer = torch.optim.AdamW(param_groups, lr=args.lr)
    loss_scaler = NativeScaler()

    ######## ######## ######## ######## ######## ######## ######## ########
    if mixup_fn is not None:
        # smoothing is handled with mixup label transform
        criterion = SoftTargetCrossEntropy()
    elif args.smoothing > 0.:
        criterion = LabelSmoothingCrossEntropy(smoothing=args.smoothing)
    else:
        criterion = torch.nn.CrossEntropyLoss()

    if args.binary_class:
        if args.binary_class_weights:
            weights = torch.tensor(args.binary_class_weights).to(device)
            criterion = torch.nn.BCEWithLogitsLoss(pos_weight=weights)
        else:
            criterion = torch.nn.BCEWithLogitsLoss()
    elif args.regression:
        criterion = torch.nn.MSELoss()
    ######## ######## ######## ######## ######## ######## ######## ########

    print("criterion = %s" % str(criterion))

    misc.load_model(args=args, model_without_ddp=model_without_ddp, optimizer=optimizer, loss_scaler=loss_scaler)

    if args.eval:
        test_stats = evaluate(data_loader_val, model, device, criterion)
        print(f"Accuracy of the network on the {len(dataset_val)} test images: {test_stats['acc1']:.1f}%")
        exit(0)

    print(f"Start training for {args.epochs} epochs")
    start_time = time.time()

    # Initialize best validation metrics
    best_metrics = {
        "best_val_loss": float('inf'),
        "best_auc": 0.0,
        "best_accuracy": 0.0,
    }

    iteration_counter = 0  # Track total iterations

    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            data_loader_train.sampler.set_epoch(epoch)

        # Training and in-iteration validation
        train_stats, best_metrics, test_stats = train_one_epoch_iter(
            model, criterion, data_loader_train,
            optimizer, device, epoch, loss_scaler,
            args.clip_grad, mixup_fn,
            log_writer=log_writer,
            args=args,
            iteration_counter=iteration_counter,
            validate_every=20,
            data_loader_val=data_loader_val,
            best_metrics=best_metrics,  # Pass the initialized best_metrics
            output_dir=args.output_dir
        )

        # Increment the iteration counter by the number of steps in this epoch
        iteration_counter += len(data_loader_train)

        # Log stats for the epoch
        log_stats = {
            **{f'train_{k}': v.global_avg for k, v in train_stats.meters.items()},
            **{f'test_{k}': v for k, v in test_stats.items()},  # Include test_stats
            'epoch': epoch,
            'n_parameters': n_parameters
        }

        if args.output_dir and misc.is_main_process():
            if log_writer is not None:
                log_writer.flush()
            with open(os.path.join(args.output_dir, "log.txt"), mode="a", encoding="utf-8") as f:
                f.write(json.dumps(log_stats) + "\n")

    # Save the final model after training is complete
    final_model_path = os.path.join(args.output_dir, 'final_model.pth')
    torch.save(model.state_dict(), final_model_path)
    print(f"Final model saved at: {final_model_path}")

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    args = get_args_parser()
    args = args.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
