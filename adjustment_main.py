from __future__ import print_function, absolute_import
import argparse
import datetime
import os
import os.path as osp
import time

import numpy as np
import sys
import torch
from tqdm import tqdm
from torch import nn
from torch.backends import cudnn
from torch.utils.data import DataLoader
from reid.datasets.domain_adaptation import DA

from reid import models
from reid.trainers import Trainer
from reid.evaluators import Evaluator
from reid.utils.data import transforms as T
from reid.utils.data.preprocessor import Preprocessor, UnsupervisedCamStylePreprocessor
from reid.utils.logging import Logger
from reid.utils.serialization import load_checkpoint, save_checkpoint
from reid.loss import InvNet


def get_data(data_dir, source, target, height, width, batch_size, re=0, workers=8, C=False):

    dataset = DA(data_dir, source, target)

    normalizer = T.Normalize(mean=[0.485, 0.456, 0.406],
                             std=[0.229, 0.224, 0.225])

    num_classes = dataset.num_train_ids

    train_transformer = T.Compose([
        T.RandomSizedRectCrop(height, width),
        T.RandomHorizontalFlip(),
        T.ToTensor(),
        normalizer,
        T.RandomErasing(EPSILON=re),
    ])

    test_transformer = T.Compose([
        T.Resize((height, width), interpolation=3),
        T.ToTensor(),
        normalizer,
    ])

    source_train_loader = DataLoader(
        Preprocessor(dataset.source_train, root=osp.join(dataset.source_images_dir, dataset.source_train_path),
                     transform=train_transformer),
        batch_size=batch_size, num_workers=workers,
        shuffle=True, pin_memory=True, drop_last=True)

    if not C:
        target_train_loader = DataLoader(
            UnsupervisedCamStylePreprocessor(dataset.target_train,
                                             root=osp.join(dataset.target_images_dir, dataset.target_train_path),
                                             camstyle_root=osp.join(dataset.target_images_dir,
                                                                    dataset.target_train_camstyle_path),
                                             num_cam=dataset.target_num_cam, transform=train_transformer),
            batch_size=batch_size, num_workers=workers,
            shuffle=True, pin_memory=True, drop_last=True)
    else:
        target_train_loader = DataLoader(
            Preprocessor(dataset.target_train,
                         root=osp.join(dataset.target_images_dir, dataset.target_train_path), transform=train_transformer),
            batch_size=batch_size, num_workers=workers,
            shuffle=True, pin_memory=True, drop_last=True)

    query_loader = DataLoader(
        Preprocessor(dataset.query,
                     root=osp.join(dataset.target_images_dir, dataset.query_path), transform=test_transformer),
        batch_size=batch_size, num_workers=workers,
        shuffle=False, pin_memory=True)

    gallery_loader = DataLoader(
        Preprocessor(dataset.gallery,
                     root=osp.join(dataset.target_images_dir, dataset.gallery_path), transform=test_transformer),
        batch_size=batch_size, num_workers=workers,
        shuffle=False, pin_memory=True)

    return dataset, num_classes, source_train_loader, target_train_loader, query_loader, gallery_loader


def main(args):
    if args.adjustment == 'feature-wise':
        args.arch = 'ft' + args.arch
    if args.adjustment == 'class-wise':
        args.arch = 'cl' + args.arch
        args.n_splits = 1
    if args.adjustment == 'Combined':
        args.arch = 'cb' + args.arch

    # For fast training.
    cudnn.benchmark = True
    if args.gpus is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = args.gpus
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # print(torch.cuda.is_available())
    # cord = torch.randn(268435456 * args.core).to(device)

    # Redirect print to both console and log file
    log_name = "{}_{}2{}_alpha{}_beta{}_knn{}_lmd{}_nsplits{}_features{}_".format(args.adjustment, args.source, args.target, args.inv_alpha, args.inv_beta, args.knn, args.lmd, args.n_splits, args.features)
    log_name = log_name + "date{}.txt".format(time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime()))
    if not args.evaluate:
        sys.stdout = Logger(osp.join(args.logs_dir, log_name))
    print('log_dir=', args.logs_dir)

    # Print logs
    print(args)

    # Create data loaders
    dataset, num_classes, source_train_loader, target_train_loader, \
    query_loader, gallery_loader = get_data(args.data_dir, args.source,
                                            args.target, args.height,
                                            args.width, args.batch_size,
                                            args.re, args.workers, C=args.C)

    # Create model
    model = models.create(args.arch, num_features=args.features,
                          dropout=args.dropout, num_classes=num_classes, n_splits=args.n_splits, batch_size=args.batch_size, adjustment=args.adjustment)

    # Invariance learning model
    num_tgt = len(dataset.target_train)
    model_inv = InvNet(args.features, num_tgt,
                        beta=args.inv_beta, knn=args.knn,
                        alpha=args.inv_alpha, n_splits=args.n_splits, N=args.N)

    # Load from checkpoint
    start_epoch = 0
    if args.resume:
        checkpoint = load_checkpoint(args.resume)
        model.load_state_dict(checkpoint['state_dict'])
        model_inv.load_state_dict(checkpoint['state_dict_inv'])
        start_epoch = checkpoint['epoch']
        print("=> Start epoch {} "
              .format(start_epoch))

    # Set model
    model = nn.DataParallel(model).to(device)
    model_inv = model_inv.to(device)

    # Evaluator
    evaluator = Evaluator(model)
    if args.evaluate:
        print("Test:")
        evaluator.evaluate(query_loader, gallery_loader, dataset.query,
                           dataset.gallery, args.output_feature)
        return

    # Optimizer
    base_param_ids = set(map(id, model.module.base.parameters()))

    base_params_need_for_grad = filter(lambda p: p.requires_grad, model.module.base.parameters())

    new_params = [p for p in model.parameters() if
                    id(p) not in base_param_ids]
    param_groups = [
        {'params': base_params_need_for_grad, 'lr_mult': 0.1},
        {'params': new_params, 'lr_mult': 1.0}]

    optimizer = torch.optim.SGD(param_groups, lr=args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay,
                                nesterov=True)

    # Trainer
    trainer = Trainer(model, model_inv, lmd=args.lmd, n_splits=args.n_splits, adjustment=args.adjustment, num_classes=num_classes, num_features=args.features, E=args.E)

    # Schedule learning rate
    def adjust_lr(epoch):
        step_size = args.epochs_decay
        lr = args.lr * (0.1 ** (epoch // step_size))
        for g in optimizer.param_groups:
            g['lr'] = lr * g.get('lr_mult', 1)

    # Start training
    for epoch in range(start_epoch, args.epochs):
        adjust_lr(epoch)
        trainer.train(epoch, source_train_loader, target_train_loader, optimizer)

        save_checkpoint({
            'state_dict': model.module.state_dict(),
            'state_dict_inv': model_inv.state_dict(),
            'epoch': epoch + 1,
        }, fpath=osp.join(args.logs_dir, 'checkpoint.pth.tar'))

        # print('\n * Finished epoch {:3d} \n'.
        #       format(epoch))
        # if epoch % 10 == 0 and epoch != 0:
        if epoch >= args.epochs - 10:
            print('Test in epoch', epoch, ':')
            evaluator = Evaluator(model)
            evaluator.evaluate(query_loader, gallery_loader, dataset.query,
                               dataset.gallery, args.output_feature)

    # Final test
    # print('Test with best model:')
    # evaluator = Evaluator(model)
    # evaluator.evaluate(query_loader, gallery_loader, dataset.query,
    #                    dataset.gallery, args.output_feature)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Invariance Learning for Domain Adaptive Re-ID")
    # source
    parser.add_argument('-s', '--source', type=str, default='duke',
                        choices=['market', 'duke', 'msmt17'])
    # target
    parser.add_argument('-t', '--target', type=str, default='market',
                        choices=['market', 'duke', 'msmt17'])

    parser.add_argument('--gpus', type=str, help='gpus')
    # imgs setting
    parser.add_argument('-b', '--batch-size', type=int, default=128)
    parser.add_argument('-j', '--workers', type=int, default=8)
    parser.add_argument('--height', type=int, default=384,
                        help="input height, default: 256")
    parser.add_argument('--width', type=int, default=128,
                        help="input width, default: 128")
    # model
    parser.add_argument('-a', '--arch', type=str, default='resnet50',
                        choices=models.names())
    parser.add_argument('--features', type=int, default=2048)
    parser.add_argument('--dropout', type=float, default=0.5)
    # optimizer
    parser.add_argument('--lr', type=float, default=0.1,
                        help="learning rate of new parameters, for ImageNet pretrained"
                             "parameters it is 10 times smaller than this")
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--weight-decay', type=float, default=5e-4)
    # training configs
    parser.add_argument('--resume', type=str, default='', metavar='PATH')
    parser.add_argument('--evaluate', action='store_true',
                        help="evaluation only")
    parser.add_argument('--epochs', type=int, default=60)
    parser.add_argument('--epochs_decay', type=int, default=40)
    parser.add_argument('--print-freq', type=int, default=1)
    # metric learning
    parser.add_argument('--dist-metric', type=str, default='euclidean')
    # misc
    working_dir = osp.dirname(osp.abspath(__file__))
    parser.add_argument('--data-dir', type=str, metavar='PATH',
                        default=osp.join(working_dir, 'data'))
    parser.add_argument('--logs-dir', type=str, metavar='PATH',
                        default=osp.join(working_dir, 'logs'))
    parser.add_argument('--output_feature', type=str, default='pool5')
    # random erasing
    parser.add_argument('--re', type=float, default=0.5)
    # Invariance learning
    parser.add_argument('--inv-alpha', type=float, default=0.01,
                        help='update rate for the exemplar memory in invariance learning')
    parser.add_argument('--inv-beta', type=float, default=0.05,
                        help='The temperature in invariance learning')
    parser.add_argument('--knn', default=6, type=int,
                        help='number of KNN for neighborhood invariance')
    parser.add_argument('--lmd', type=float, default=0.8,
                        help='weight controls the importance of the source loss and the target loss.')
    parser.add_argument('--n_splits', default=8, type=int,
                        help='把 feature 分成 n_split 层')
    parser.add_argument('--adjustment', default='feature-wise', type=str,
                        help='调整方式')

    parser.add_argument('--E', action='store_true', default=False, help='close Exemplar-invariance')
    parser.add_argument('--C', action='store_true', default=False, help='close Camera-invariance')
    parser.add_argument('--N', action='store_true', default=False, help='close Neighborhood-invariance')

    args = parser.parse_args()
    main(args)
