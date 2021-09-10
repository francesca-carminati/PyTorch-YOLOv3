#! /usr/bin/env python3

from __future__ import division

import os
import argparse
import tqdm

import torch
from torch.utils.data import DataLoader
import torch.optim as optim

from pytorchyolo.models import load_model
from pytorchyolo.utils.logger import Logger
from pytorchyolo.utils.utils import to_cpu, load_classes, print_environment_info, provide_determinism, worker_seed_set
from pytorchyolo.utils.datasets import ListDataset
from pytorchyolo.utils.augmentations import AUGMENTATION_TRANSFORMS
# from pytorchyolo.utils.transforms import DEFAULT_TRANSFORMS
from pytorchyolo.utils.parse_config import parse_data_config
from pytorchyolo.utils.loss import compute_loss
from pytorchyolo.test import _evaluate, _create_validation_data_loader

from terminaltables import AsciiTable

from torchsummary import summary

from sacred import Experiment
from sacred.observers import MongoObserver

ex = Experiment()
ex.observers.append(MongoObserver(
    url='mongodb://sample:password@localhost/?authMechanism=SCRAM-SHA-1',
    db_name='db'))

def _create_data_loader(img_path, batch_size, img_size, n_cpu, multiscale_training=False):
    """Creates a DataLoader for training.

    :param img_path: Path to file containing all paths to training images.
    :type img_path: str
    :param batch_size: Size of each image batch
    :type batch_size: int
    :param img_size: Size of each image dimension for yolo
    :type img_size: int
    :param n_cpu: Number of cpu threads to use during batch generation
    :type n_cpu: int
    :param multiscale_training: Scale images to different sizes randomly
    :type multiscale_training: bool
    :return: Returns DataLoader
    :rtype: DataLoader
    """
    dataset = ListDataset(
        img_path,
        img_size=img_size,
        multiscale=multiscale_training,
        transform=AUGMENTATION_TRANSFORMS)
    dataloader = DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=n_cpu,
        pin_memory=True,
        collate_fn=dataset.collate_fn,
        worker_init_fn=worker_seed_set)
    return dataloader

@ex.config
def my_config():
    #parser = argparse.ArgumentParser(description="Trains the YOLO model.")
    #parser.add_argument("-m", "--model", type=str, default="config/yolov3.cfg", help="Path to model definition file (.cfg)")
    #parser.add_argument("-d", "--data", type=str, default="config/coco.data", help="Path to data config file (.data)")
    #parser.add_argument("-e", "--epochs", type=int, default=300, help="Number of epochs")
    #parser.add_argument("-v", "--verbose", action='store_true', help="Makes the training more verbose")
    #parser.add_argument("--n_cpu", type=int, default=8, help="Number of cpu threads to use during batch generation")
    #parser.add_argument("--pretrained_weights", type=str, help="Path to checkpoint file (.weights or .pth). Starts training from checkpoint model")
    #parser.add_argument("--checkpoint_interval", type=int, default=1, help="Interval of epochs between saving model weights")
    #parser.add_argument("--evaluation_interval", type=int, default=1, help="Interval of epochs between evaluations on validation set")
    #parser.add_argument("--multiscale_training", action="store_false", help="Allow for multi-scale training")
    # parser.add_argument("--iou_thres", type=float, default=0.5, help="Evaluation: IOU threshold required to qualify as detected")
    # parser.add_argument("--conf_thres", type=float, default=0.1, help="Evaluation: Object confidence threshold")
    # parser.add_argument("--nms_thres", type=float, default=0.5, help="Evaluation: IOU threshold for non-maximum suppression")
    # parser.add_argument("--logdir", type=str, default="logs", help="Directory for training log files (e.g. for TensorBoard)")
    # parser.add_argument("--seed", type=int, default=-1, help="Makes results reproducable. Set -1 to disable.")
    # args = parser.parse_args()

    model = "config/yolov3.cfg"
    data = "config/coco.data"
    epochs = 300
    verbose = True
    n_cpu = 8 
    pretrained_weights = ""
    checkpoint_interval = 1
    evaluation_interval = 1
    multiscale_training = False
    iou_thres = 0.1
    conf_thres = 0.1
    nms_thres = 0.5 
    logdir = "logs"
    seed = 2


    

@ex.automain
def run(model,
        data,
        epochs,
        verbose,
        n_cpu,
        pretrained_weights,
        checkpoint_interval,
        evaluation_interval,
        multiscale_training,
        iou_thres,
        conf_thres,
        nms_thres,
        logdir,
        seed):
    print_environment_info()
    
    #print(f"Command line arguments: {args}")

    if seed != -1:
        provide_determinism(seed)

    logger = Logger(logdir)  # Tensorboard logger

    # Create output directories if missing
    os.makedirs("output", exist_ok=True)
    os.makedirs("checkpoints", exist_ok=True)

    # Get data configuration
    data_config = parse_data_config(data)
    train_path = data_config["train"]
    valid_path = data_config["valid"]
    class_names = load_classes(data_config["names"])
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # ############
    # Create model
    # ############

    model = load_model(model, pretrained_weights)

    # Print model
    if verbose:
        summary(model, input_size=(3, model.hyperparams['height'], model.hyperparams['height']))

    mini_batch_size = model.hyperparams['batch'] // model.hyperparams['subdivisions']

    # #################
    # Create Dataloader
    # #################

    # Load training dataloader
    training_dataloader = _create_data_loader(
        train_path,
        mini_batch_size,
        model.hyperparams['height'],
        n_cpu,
        multiscale_training)

    # Load validation dataloader
    validation_dataloader = _create_validation_data_loader(
        valid_path,
        mini_batch_size,
        model.hyperparams['height'],
        n_cpu)

    # ################
    # Create optimizer
    # ################

    params = [p for p in model.parameters() if p.requires_grad]

    if (model.hyperparams['optimizer'] in [None, "adam"]):
        optimizer = optim.Adam(
            params,
            lr=model.hyperparams['learning_rate'],
            weight_decay=model.hyperparams['decay'],
        )
    elif (model.hyperparams['optimizer'] == "sgd"):
        optimizer = optim.SGD(
            params,
            lr=model.hyperparams['learning_rate'],
            weight_decay=model.hyperparams['decay'],
            momentum=model.hyperparams['momentum'])
    else:
        print("Unknown optimizer. Please choose between (adam, sgd).")

    # Enable data parallelism
    #model = torch.nn.DataParallel(model)

    for epoch in range(epochs):

        print("\n---- Training Model ----")

        model.train()  # Set model to training mode
        best_mAP = 0
        for batch_i, (_, imgs, targets) in enumerate(tqdm.tqdm(training_dataloader, desc=f"Training Epoch {epoch}")):
            batches_done = len(training_dataloader) * epoch + batch_i

            imgs = imgs.to(device, non_blocking=True)
            targets = targets.to(device)

            outputs = model(imgs)

            loss, loss_components = compute_loss(outputs, targets, model)

            loss.backward()

            ###############
            # Run optimizer
            ###############

            if batches_done % model.hyperparams['subdivisions'] == 0:
                # Adapt learning rate
                # Get learning rate defined in cfg
                lr = model.hyperparams['learning_rate']
                if batches_done < model.hyperparams['burn_in']:
                    # Burn in
                    lr *= (batches_done / model.hyperparams['burn_in'])
                else:
                    # Set and parse the learning rate to the steps defined in the cfg
                    for threshold, value in model.hyperparams['lr_steps']:
                        if batches_done > threshold:
                            lr *= value
                # Log the learning rate
                logger.scalar_summary("train/learning_rate", lr, batches_done)
                # Set learning rate
                for g in optimizer.param_groups:
                    g['lr'] = lr

                # Run optimizer
                optimizer.step()
                # Reset gradients
                optimizer.zero_grad()

            # ############
            # Log progress
            # ############
            if verbose:
                print(AsciiTable(
                    [
                        ["Type", "Value"],
                        ["IoU loss", float(loss_components[0])],
                        ["Object loss", float(loss_components[1])],
                        ["Class loss", float(loss_components[2])],
                        ["Loss", float(loss_components[3])],
                        ["Batch loss", to_cpu(loss).item()],
                    ]).table)

            # Tensorboard logging
            tensorboard_log = [
                ("train/iou_loss", float(loss_components[0])),
                ("train/obj_loss", float(loss_components[1])),
                ("train/class_loss", float(loss_components[2])),
                ("train/loss", to_cpu(loss).item())]


            logger.list_of_scalars_summary(tensorboard_log, batches_done)

            model.seen += imgs.size(0)
        
        # Sacred Logging Training loss
        
        ex.log_scalar("train-loss", to_cpu(loss).item(), epoch+1)

        # #############
        # Save progress
        # #############

        # Save model to checkpoint file
        if epoch % checkpoint_interval == 0:
            checkpoint_path = f"checkpoints/yolov3_ckpt_{epoch}.pth"
            print(f"---- Saving checkpoint to: '{checkpoint_path}' ----")
            torch.save(model.state_dict(), checkpoint_path)

        # ########
        # Evaluate
        # ########

        if epoch % evaluation_interval == 0:
            print("\n---- Evaluating Model ----")

            for subset in ['training', 'validation']:
                print('Evaluate ' + subset + ' set')
                dataloader = training_dataloader if subset == 'training' else validation_dataloader

                # Evaluate the model on the validation set
                metrics_output = _evaluate(
                    model,
                    dataloader,
                    class_names,
                    img_size=model.hyperparams['height'],
                    iou_thres=iou_thres,
                    conf_thres=conf_thres,
                    nms_thres=nms_thres,
                    verbose=verbose
                )

                if metrics_output is not None:
                    precision, recall, AP, f1, ap_class = metrics_output
                    evaluation_metrics = [
                        ("precision", precision.mean()),
                        ("recall", recall.mean()),
                        ("mAP", AP.mean()),
                        ("f1", f1.mean())]
                    
                    # Log all metrics in sacred
                    for metric in evaluation_metrics:
                        ex.log_scalar(f"{subset}.{metric[0]}", metric[1], epoch+1)
                        logger.list_of_scalars_summary(evaluation_metrics, epoch)
                #Save best checkpoint
                if subset == 'validation':
                    mAP = evaluation_metrics[2][1]
                    if mAP > best_mAP:
                        checkpoint_path = f"checkpoints/best_ckpt.pth"
                        print(f"---- Saving best checkpoint to: '{checkpoint_path}' ----")
                        torch.save(model.state_dict(), checkpoint_path)
#####################
####################
            for batch_i, (_, imgs, targets) in enumerate(tqdm.tqdm(training_dataloader, desc=f"Training Epoch {epoch}")):
                #batches_done = len(training_dataloader) * epoch + batch_i

                imgs = imgs.to(device, non_blocking=True)
                targets = targets.to(device)

                outputs = model(imgs)

                loss, loss_components = compute_loss(outputs, targets, model)
#####################
####################
            
            for _, imgs, targets in tqdm.tqdm(validation_dataloader, desc="Computing val loss"):
                imgs = imgs.to(device, non_blocking=True)
                targets = targets.to(device)

                outputs = model(imgs)

                loss, loss_components = compute_loss(outputs, targets, model)
            ex.log_scalar("val-loss", to_cpu(loss).item(), epoch+1)


            


#if __name__ == "__main__":
 #   run(args)
