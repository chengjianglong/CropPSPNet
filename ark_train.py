import os
import sys
import yaml
import time
import shutil
import torch
import visdom
import random
import argparse
import datetime
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models

from torch.utils import data
from tqdm import tqdm

import json
from ptsemseg.myutils.config import Config
from ptsemseg.myutils.utils import update_config
from ptsemseg.tasks.transforms import augment_flips_color, augment_multiple_operations
from ptsemseg.dataset.image_provider import ImageProvider
from ptsemseg.dataset.threeband_image import ThreebandImageType
from ptsemseg.dataset.multiband_image import MultibandImageType
from ptsemseg.dataset.neural_dataset import TrainDataset, ValDataset

from ptsemseg.models import get_model
from ptsemseg.loss import get_loss_function
from ptsemseg.loader import get_loader 
from ptsemseg.utils import get_logger
from ptsemseg.metrics import runningScore, averageMeter
from ptsemseg.augmentations import get_composed_augmentations
from ptsemseg.schedulers import get_scheduler
from ptsemseg.optimizers import get_optimizer

from tensorboardX import SummaryWriter

def train(cfg, writer, logger):
    
    # Setup seeds
    torch.manual_seed(cfg.get('seed', 1337))
    torch.cuda.manual_seed(cfg.get('seed', 1337))
    np.random.seed(cfg.get('seed', 1337))
    random.seed(cfg.get('seed', 1337))

    # Setup device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Setup Augmentations
    augmentations = cfg['training'].get('augmentations', None)
    data_aug = get_composed_augmentations(augmentations)

    # Setup Dataloader
#    data_loader = get_loader(cfg['data']['dataset'])
#    data_path = cfg['data']['path']
#
#    t_loader = data_loader(
#        data_path,
#        is_transform=True,
#        split=cfg['data']['train_split'],
#        img_size=(cfg['data']['img_rows'], cfg['data']['img_cols']),
#        augmentations=data_aug)
#
#    v_loader = data_loader(
#        data_path,
#        is_transform=True,
#        split=cfg['data']['val_split'],
#        img_size=(cfg['data']['img_rows'], cfg['data']['img_cols']),)
#
#    n_classes = t_loader.n_classes
#    trainloader = data.DataLoader(t_loader,
#                                  batch_size=cfg['training']['batch_size'], 
#                                  num_workers=cfg['training']['n_workers'], 
#                                  shuffle=True)
#
#    valloader = data.DataLoader(v_loader, 
#                                batch_size=cfg['training']['batch_size'], 
#                                num_workers=cfg['training']['n_workers'])

    paths = {
        'masks': './satellitedata/patchark_train/gt/',
        'images': './satellitedata/patchark_train/rgb',
        'nirs': './satellitedata/patchark_train/nir',
        'swirs': './satellitedata/patchark_train/swir',
        'vhs': './satellitedata/patchark_train/vh',
        'vvs': './satellitedata/patchark_train/vv',
        'redes': './satellitedata/patchark_train/rede',
        'ndvis': './satellitedata/patchark_train/ndvi',
        }

    valpaths = {
        'masks': './satellitedata/patchark_val/gt/',
        'images': './satellitedata/patchark_val/rgb',
        'nirs': './satellitedata/patchark_val/nir',
        'swirs': './satellitedata/patchark_val/swir',
        'vhs': './satellitedata/patchark_val/vh',
        'vvs': './satellitedata/patchark_val/vv',
        'redes': './satellitedata/patchark_val/rede',
        'ndvis': './satellitedata/patchark_val/ndvi',
        }
  
  
    n_classes = 3
    train_img_paths = [pth for pth in os.listdir(paths['images']) if ('_01_' not in pth) and ('_25_' not in pth)]
    val_img_paths = [pth for pth in os.listdir(valpaths['images']) if ('_01_' not in pth) and ('_25_' not in pth)]
    ntrain = len(train_img_paths)
    nval = len(val_img_paths)
    train_idx = [i for i in range(ntrain)]
    val_idx = [i for i in range(nval)]
    trainds = ImageProvider(MultibandImageType, paths, image_suffix='.png')
    valds = ImageProvider(MultibandImageType, valpaths, image_suffix='.png')
    
    config_path = 'crop_pspnet_config.json'
    with open(config_path, 'r') as f:
        mycfg = json.load(f)
        train_data_path = './satellitedata/'
        print('train_data_path: {}'.format(train_data_path))
        dataset_path, train_dir = os.path.split(train_data_path)
        print('dataset_path: {}'.format(dataset_path) + ',  train_dir: {}'.format(train_dir))
        mycfg['dataset_path'] = dataset_path
    config = Config(**mycfg)

    config = update_config(config, num_channels=12, nb_epoch=50)
    #dataset_train = TrainDataset(trainds, train_idx, config, transforms=augment_flips_color)
    dataset_train = TrainDataset(trainds, train_idx, config, 1)
    dataset_val = TrainDataset(valds, val_idx, config, 1)
    trainloader = data.DataLoader(dataset_train,
                                  batch_size=cfg['training']['batch_size'], 
                                  num_workers=cfg['training']['n_workers'], 
                                  shuffle=True)

    valloader = data.DataLoader(dataset_val,
                                  batch_size=cfg['training']['batch_size'], 
                                  num_workers=cfg['training']['n_workers'], 
                                  shuffle=False)
    # Setup Metrics
    running_metrics_train = runningScore(n_classes)
    running_metrics_val = runningScore(n_classes)

    k = 0
    nbackground = 0
    ncorn = 0
    #ncotton = 0
    #nrice = 0
    nsoybean = 0


    for indata in trainloader:
        k += 1
        gt = indata['seg_label'].data.cpu().numpy()
        nbackground += (gt == 0).sum()
        ncorn += (gt == 1).sum()
        #ncotton += (gt == 2).sum()
        #nrice += (gt == 3).sum()
        nsoybean += (gt == 2).sum()

    print('k = {}'.format(k))
    print('nbackgraound: {}'.format(nbackground))
    print('ncorn: {}'.format(ncorn))
    #print('ncotton: {}'.format(ncotton))
    #print('nrice: {}'.format(nrice))
    print('nsoybean: {}'.format(nsoybean))
    
    wgts = [1.0, 1.0*nbackground/ncorn, 1.0*nbackground/nsoybean]
    total_wgts = sum(wgts)
    wgt_background = wgts[0]/total_wgts
    wgt_corn = wgts[1]/total_wgts
    #wgt_cotton = wgts[2]/total_wgts
    #wgt_rice = wgts[3]/total_wgts
    wgt_soybean = wgts[2]/total_wgts
    weights = torch.autograd.Variable(torch.cuda.FloatTensor([wgt_background, wgt_corn, wgt_soybean]))

    #weights = torch.autograd.Variable(torch.cuda.FloatTensor([1.0, 1.0, 1.0]))
    

    # Setup Model
    model = get_model(cfg['model'], n_classes).to(device)

    model = torch.nn.DataParallel(model, device_ids=range(torch.cuda.device_count()))

    # Setup optimizer, lr_scheduler and loss function
    optimizer_cls = get_optimizer(cfg)
    optimizer_params = {k:v for k, v in cfg['training']['optimizer'].items() 
                        if k != 'name'}

    optimizer = optimizer_cls(model.parameters(), **optimizer_params)
    logger.info("Using optimizer {}".format(optimizer))

    scheduler = get_scheduler(optimizer, cfg['training']['lr_schedule'])

    loss_fn = get_loss_function(cfg)
    logger.info("Using loss {}".format(loss_fn))

    start_iter = 0
    if cfg['training']['resume'] is not None:
        if os.path.isfile(cfg['training']['resume']):
            logger.info(
                "Loading model and optimizer from checkpoint '{}'".format(cfg['training']['resume'])
            )
            checkpoint = torch.load(cfg['training']['resume'])
            model.load_state_dict(checkpoint["model_state"])
            optimizer.load_state_dict(checkpoint["optimizer_state"])
            scheduler.load_state_dict(checkpoint["scheduler_state"])
            start_iter = checkpoint["epoch"]
            logger.info(
                "Loaded checkpoint '{}' (iter {})".format(
                    cfg['training']['resume'], checkpoint["epoch"]
                )
            )
        else:
            logger.info("No checkpoint found at '{}'".format(cfg['training']['resume']))

    val_loss_meter = averageMeter()
    time_meter = averageMeter()

    best_iou = -100.0
    i = start_iter
    flag = True

    while i <= cfg['training']['train_iters'] and flag:
        for inputdata in trainloader:
            i += 1
            start_ts = time.time()
            scheduler.step()
            model.train()
            images = inputdata['img_data']
            labels = inputdata['seg_label']
            #print('images.size: {}'.format(images.size()))
            #print('labels.size: {}'.format(labels.size()))
            images = images.to(device)
            labels = labels.to(device)

            optimizer.zero_grad()
            outputs = model(images)

            #print('outputs.size: {}'.format(outputs[1].size()))
            #print('labels.size: {}'.format(labels.size()))

            loss = loss_fn(input=outputs[1], target=labels, weight=weights)

            loss.backward()
            optimizer.step()
            
            time_meter.update(time.time() - start_ts)

            if (i + 1) % cfg['training']['print_interval'] == 0:
                fmt_str = "Iter [{:d}/{:d}]  Loss: {:.4f}  Time/Image: {:.4f}"
                print_str = fmt_str.format(i + 1,
                                           cfg['training']['train_iters'], 
                                           loss.item(),
                                           time_meter.avg / cfg['training']['batch_size'])

                print(print_str)
                logger.info(print_str)
                writer.add_scalar('loss/train_loss', loss.item(), i+1)
                time_meter.reset()

            if (i + 1) % cfg['training']['val_interval'] == 0 or \
               (i + 1) == cfg['training']['train_iters']:
                model.eval()
                with torch.no_grad():
                    for inputdata in valloader:
                        images_val = inputdata['img_data']
                        labels_val = inputdata['seg_label']
                        images_val = images_val.to(device)
                        labels_val = labels_val.to(device)

                        outputs = model(images_val)
                        val_loss = loss_fn(input=outputs, target=labels_val)

                        pred = outputs.data.max(1)[1].cpu().numpy()
                        gt = labels_val.data.cpu().numpy()


                        running_metrics_val.update(gt, pred)
                        val_loss_meter.update(val_loss.item())

                writer.add_scalar('loss/val_loss', val_loss_meter.avg, i+1)
                logger.info("Iter %d Loss: %.4f" % (i + 1, val_loss_meter.avg))

                score, class_iou = running_metrics_val.get_scores()
                for k, v in score.items():
                    print(k, v)
                    logger.info('{}: {}'.format(k, v))
                    writer.add_scalar('val_metrics/{}'.format(k), v, i+1)

                for k, v in class_iou.items():
                    logger.info('{}: {}'.format(k, v))
                    writer.add_scalar('val_metrics/cls_{}'.format(k), v, i+1)

                val_loss_meter.reset()
                running_metrics_val.reset()

                if score["Mean IoU : \t"] >= best_iou:
                    best_iou = score["Mean IoU : \t"]
                    state = {
                        "epoch": i + 1,
                        "model_state": model.state_dict(),
                        "optimizer_state": optimizer.state_dict(),
                        "scheduler_state": scheduler.state_dict(),
                        "best_iou": best_iou,
                    }
                    save_path = os.path.join(writer.file_writer.get_logdir(),
                                             "{}_{}_best_model.pkl".format(
                                                 cfg['model']['arch'],
                                                 cfg['data']['dataset']))
                    torch.save(state, save_path)

            if (i + 1) == cfg['training']['train_iters']:
                flag = False
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="config")
    parser.add_argument(
        "--config",
        nargs="?",
        type=str,
        default="configs/pspnet_crop_ark.yml",
        help="Configuration file to use"
    )

    args = parser.parse_args()

    with open(args.config) as fp:
        cfg = yaml.load(fp)

    run_id = random.randint(1,100000)
    logdir = os.path.join('runs', os.path.basename(args.config)[:-4] , str(run_id))
    writer = SummaryWriter(log_dir=logdir)

    print('RUNDIR: {}'.format(logdir))
    shutil.copy(args.config, logdir)

    logger = get_logger(logdir)
    logger.info('Let the games begin')

    train(cfg, writer, logger)
