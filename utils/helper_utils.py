import os
from os import listdir
from os.path import splitext
from glob import glob

import copy

import logging

import numpy as np

import cv2
from PIL import Image
import matplotlib.pyplot as plt

from shapely.geometry import Polygon

import torch
import torch.nn.functional as F

######################
######################

import cfg as config

######################
######################

from model.utils import bbox_utils
from utils import bbox_utils as _bbox_utils

from dataset.utils.COCO import coco_utils
from dataset.utils.UMD import umd_utils
from dataset.utils.Elevator import elevator_utils
from dataset.utils.ARLVicon import arl_vicon_dataset_utils
from dataset.utils.ARLAffPose import affpose_dataset_utils

######################
# IMG UTILS
######################

def convert_16_bit_depth_to_8_bit(depth):
    depth = np.array(depth, np.uint16)
    depth = depth / np.max(depth) * (2 ** 8 - 1)
    return np.array(depth, np.uint8)

def print_depth_info(depth):
    depth = np.array(depth)
    print(f"Depth of type:{depth.dtype} has min:{np.min(depth)} & max:{np.max(depth)}")

def print_class_labels(seg_mask):
    class_ids = np.unique(np.array(seg_mask, dtype=np.uint8))[1:]  # exclude the background
    print(f"Mask has {len(class_ids)} Labels: {class_ids}")

def print_class_obj_names(obj_labels):
    print('')
    for obj_label in obj_labels:
        _object = affpose_dataset_utils.map_obj_id_to_name(obj_label)
        print(f"Obj Id:{obj_label}, Object: {_object}")

def print_class_aff_names(aff_labels):
    print('')
    for aff_label in aff_labels:
        _affordance = affpose_dataset_utils.map_aff_id_to_name(aff_label)
        print(f"Aff Id:{aff_label}, Affordance: {_affordance}")

######################
######################

def crop(pil_img, crop_size, is_img=False):
    _dtype = np.array(pil_img).dtype
    pil_img = Image.fromarray(pil_img)
    crop_w, crop_h = crop_size
    img_width, img_height = pil_img.size
    left, right = (img_width - crop_w) / 2, (img_width + crop_w) / 2
    top, bottom = (img_height - crop_h) / 2, (img_height + crop_h) / 2
    left, top = round(max(0, left)), round(max(0, top))
    right, bottom = round(min(img_width - 0, right)), round(min(img_height - 0, bottom))
    # pil_img = pil_img.crop((left, top, right, bottom)).resize((crop_w, crop_h))
    pil_img = pil_img.crop((left, top, right, bottom))
    ###
    if is_img:
        img_channels = np.array(pil_img).shape[-1]
        img_channels = 3 if img_channels == 4 else img_channels
        resize_img = np.zeros((crop_h, crop_w, img_channels))
        resize_img[0:(bottom - top), 0:(right - left), :img_channels] = np.array(pil_img)[..., :img_channels]
    else:
        resize_img = np.zeros((crop_h, crop_w))
        resize_img[0:(bottom - top), 0:(right - left)] = np.array(pil_img)

    return np.array(resize_img, dtype=_dtype)

######################
# FORMAT UTILS
######################

def format_target_data(image, target):
    height, width = image.shape[:2]

    target['gt_mask'] = np.array(target['gt_mask'], dtype=np.uint8).reshape(height, width)
    target['labels'] = np.array(target['labels'], dtype=np.int32).flatten()
    target['boxes'] = np.array(target['boxes'], dtype=np.int32).reshape(-1, 4)
    target['masks'] = np.array(target['masks'], dtype=np.uint8).reshape(-1, height, width)

    if 'obj_labels' in target.keys():
        target['obj_labels'] = np.array(target['obj_labels'], dtype=np.int32).flatten()

    if 'aff_labels' in target.keys():
        target['aff_labels'] = np.array(target['aff_labels'], dtype=np.int32).flatten()

    return target

def format_label(label):
    return np.array(label, dtype=np.int32)

def format_bbox(bbox):
    return np.array(bbox, dtype=np.int32).flatten()

######################
# ANCHOR UTILS
######################

def visualize_anchors(image):
    anchor_img = image.copy()

    # prelim
    image_shape = image.shape[:2]
    stride_length = 16
    resent_50_features = torch.randn(1, 256,
                                     int(image_shape[0]/stride_length),
                                     int(image_shape[1]/stride_length),
                                     device='cpu')

    # rpn
    rpn_anchor_generator = bbox_utils.AnchorGenerator(config.ANCHOR_SIZES, config.ANCHOR_RATIOS)
    anchors = rpn_anchor_generator(resent_50_features, image_shape)

    for anchor in anchors:
        anchor = np.array(anchor, dtype=np.int32)

        # clip anchors
        height, width = image.shape[:2]
        anchor[0] = np.max([0, anchor[0]])  # x1
        anchor[1] = np.max([0, anchor[1]])  # y1
        anchor[2] = np.min([width, anchor[2]])  # x2
        anchor[3] = np.min([height, anchor[3]])  # y2

        # clip anchors
        x1 = height // 2 - np.abs(anchor[0])
        y1 = width // 2 - np.abs(anchor[1])
        x2 = height // 2 + np.abs(anchor[2])
        y2 = width // 2 + np.abs(anchor[3])
        # x1,y1 ------
        # |          |
        # |          |
        # |          |
        # --------x2,y2
        _color = (np.random.randint(0, 255), np.random.randint(0, 255), np.random.randint(0, 255))
        anchor_img = cv2.rectangle(anchor_img, (x1, y1), (x2, y2), _color, 1)
        print(f'x1:{x1}, y1:{y1}, x2:{x2}, y2:{y2}')
    return anchor_img

######################
######################

def get_iou(pred_box, gt_box):
    """
    pred_box : the coordinate for predict bounding box
    gt_box :   the coordinate for ground truth bounding box
    return :   the iou score
    the  left-down coordinate of  pred_box:(pred_box[0], pred_box[1])
    the  right-up coordinate of  pred_box:(pred_box[2], pred_box[3])
    """
    # 1.get the coordinate of inters
    ixmin = max(pred_box[0], gt_box[0])
    ixmax = min(pred_box[2], gt_box[2])
    iymin = max(pred_box[1], gt_box[1])
    iymax = min(pred_box[3], gt_box[3])

    iw = np.maximum(ixmax-ixmin+1., 0.)
    ih = np.maximum(iymax-iymin+1., 0.)

    # 2. calculate the area of inters
    inters = iw*ih

    # 3. calculate the area of union
    uni = ((pred_box[2]-pred_box[0]+1.) * (pred_box[3]-pred_box[1]+1.) +
           (gt_box[2] - gt_box[0] + 1.) * (gt_box[3] - gt_box[1] + 1.) -
           inters)

    # 4. calculate the overlaps between pred_box and gt_box
    iou = inters / uni

    return iou

######################
# MaskRCNN UTILS
######################

def draw_bbox_on_img(image, labels, boxes, scores=None, is_gt=False):
    bbox_img = image.copy()

    if is_gt:
        for label, bbox in zip(labels, boxes):
            bbox = format_bbox(bbox)
            # x1,y1 ------
            # |          |
            # |          |
            # |          |
            # --------x2,y2
            bbox_img = cv2.rectangle(bbox_img, (bbox[0], bbox[1]), (bbox[2], bbox[3]), 255, 1)

            cv2.putText(bbox_img,
                        # coco_utils.object_id_to_name(label),
                        # umd_utils.object_id_to_name(label),
                        # umd_utils.aff_id_to_name(label),
                        # elevator_utils.object_id_to_name(label),
                        # arl_vicon_dataset_utils.map_obj_id_to_name(label),
                        affpose_dataset_utils.map_obj_id_to_name(label),
                        # affpose_dataset_utils.map_aff_id_to_name(label),
                        (bbox[0], bbox[1] - 5),
                        cv2.FONT_ITALIC,
                        0.4,
                        (255, 255, 255))

    else:
        for idx, score in enumerate(scores):
            if score > config.CONFIDENCE_THRESHOLD:
                bbox = format_bbox(boxes[idx])
                bbox_img = cv2.rectangle(bbox_img, (bbox[0], bbox[1]), (bbox[2], bbox[3]), 255, 1)

                label = labels[idx]
                cv2.putText(bbox_img,
                            # coco_utils.object_id_to_name(label),
                            # umd_utils.object_id_to_name(label),
                            # umd_utils.aff_id_to_name(label),
                            # elevator_utils.object_id_to_name(label),
                            # arl_vicon_dataset_utils.map_obj_id_to_name(label),
                            affpose_dataset_utils.map_obj_id_to_name(label),
                            # affpose_dataset_utils.map_aff_id_to_name(label),
                            (bbox[0], bbox[1] - 5),
                            cv2.FONT_ITALIC,
                            0.4,
                            (255, 255, 255))

    return bbox_img

def get_segmentation_masks(image, labels, binary_masks, scores=None, is_gt=False):

    height, width = image.shape[:2]
    # print(f'height:{height}, width:{width}')

    instance_masks = np.zeros((height, width), dtype=np.uint8)
    instance_mask_one = np.ones((height, width), dtype=np.uint8)

    if len(binary_masks.shape) == 2:
        binary_masks = binary_masks[np.newaxis, :, :]

    if is_gt:
        for idx, label in enumerate(labels):
            binary_mask = binary_masks[idx, :, :]

            instance_mask = instance_mask_one * label
            instance_masks = np.where(binary_mask, instance_mask, instance_masks).astype(np.uint8)

    else:
        for idx, label in enumerate(labels):
            label = labels[idx]
            binary_mask = np.array(binary_masks[idx, :, :], dtype=np.uint8)

            instance_mask = instance_mask_one * label
            instance_masks = np.where(binary_mask, instance_mask, instance_masks).astype(np.uint8)

    # print_class_labels(instance_masks)
    return instance_masks

def get_obj_part_mask(image, obj_ids, bboxs, aff_ids, binary_masks, scores):

    height, width = image.shape[:2]
    # print(f'height:{height}, width:{width}')

    instance_masks = np.zeros((height, width), dtype=np.uint8)
    instance_mask_one = np.ones((height, width), dtype=np.uint8)

    if len(binary_masks.shape) == 2:
        binary_masks = binary_masks[np.newaxis, :, :]

    for idx, aff_id in enumerate(aff_ids):

        binary_mask = np.array(binary_masks[idx, :, :], dtype=np.uint8)
        # cv2.imshow('binary_mask', binary_mask * 255)
        # cv2.waitKey(0)

        try:
            obj_part_bbox = _bbox_utils.get_obj_bbox(mask=binary_mask, obj_ids=np.array([1]), img_width=height, img_height=width)[0]

            best_iou, best_idx = -np.inf, None
            for bbox_idx, bbox in enumerate(bboxs):
                score = scores[bbox_idx]
                if score > config.CONFIDENCE_THRESHOLD:
                    iou = get_iou(pred_box=obj_part_bbox, gt_box=bbox)
                    if iou > best_iou:
                        best_iou = iou
                        best_idx = bbox_idx

            obj_id = obj_ids[best_idx]
            obj_part_id = affpose_dataset_utils.map_obj_id_and_aff_id_to_obj_part_ids(obj_id, aff_id)
            # print(f'\tobj_id:{affpose_dataset_utils.map_obj_id_to_name(obj_id)}')
            # print(f'\tobj_part_id:{obj_part_id}')

            instance_mask = instance_mask_one * obj_part_id
            instance_masks = np.where(binary_mask, instance_mask, instance_masks).astype(np.uint8)
        except:
            pass

    return instance_masks





