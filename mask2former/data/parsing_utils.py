import logging
import numpy as np
from typing import List, Union
import pycocotools.mask as mask_util
import torch
from PIL import Image

from detectron2.structures import (
    BitMasks,
    Boxes,
    BoxMode,
    Instances,
    Keypoints,
    PolygonMasks,
    RotatedBoxes,
    polygons_to_bitmask,
)
from detectron2.data import transforms as T
from detectron2.data import MetadataCatalog

from pycocotools import mask as maskUtils
import random, cv2

__all__ = [
    "get_parsing_flip_map",
    "flip_parsing_semantic_category",
    "transform_parsing_instance_annotations",
    "flip_parsing_instance_category",
    "compute_parsing_IoP",
    "affine_to_target_size",
    "center_to_target_size",
]


def get_parsing_flip_map(dataset_names):
    meta = MetadataCatalog.get(dataset_names[0])
    return meta.flip_map


def flip_parsing_semantic_category(img, gt, flip_map, prob):
    do_hflip = random.random() < prob
    if do_hflip:
        img = np.flip(img, axis=1)
        gt = gt[:, ::-1]
        gt = np.ascontiguousarray(gt)
        for ori_label, new_label in flip_map:
            left = gt == ori_label
            right = gt == new_label
            gt[left] = new_label
            gt[right] = ori_label
    return img, gt


def transform_parsing_instance_annotations(
    annotation, transforms, image_size, *, parsing_flip_map=None
):
    """
    Apply transforms to box and segmentation of a single human part instance.

    It will use `transforms.apply_box` for the box, and
    `transforms.apply_coords` for segmentation polygons & keypoints.
    If you need anything more specially designed for each data structure,
    you'll need to implement your own version of this function or the transforms.

    Args:
        annotation (dict): dict of instance annotations for a single instance.
            It will be modified in-place.
        transforms (TransformList or list[Transform]):
        image_size (tuple): the height, width of the transformed image
        parsing_flip_map (tuple(int, int)): hflip label map.

    Returns:
        dict:
            the same input dict with fields "bbox", "segmentation"
            transformed according to `transforms`.
            The "bbox_mode" field will be set to XYXY_ABS.
    """
    if isinstance(transforms, (tuple, list)):
        transforms = T.TransformList(transforms)
    # bbox is 1d (per-instance bounding box)
    bbox = BoxMode.convert(annotation["bbox"], annotation["bbox_mode"], BoxMode.XYXY_ABS)
    # clip transformed bbox to image size
    bbox = transforms.apply_box(np.array([bbox]))[0].clip(min=0)
    annotation["bbox"] = np.minimum(bbox, list(image_size + image_size)[::-1])
    annotation["bbox_mode"] = BoxMode.XYXY_ABS

    if "segmentation" in annotation:
        # each instance contains 1 or more polygons
        segm = annotation["segmentation"]

        if isinstance(segm, list):
            # polygons
            polygons = [np.asarray(p).reshape(-1, 2) for p in segm]
            annotation["segmentation"] = [
                p.reshape(-1) for p in transforms.apply_polygons(polygons)
            ]

            # change part label if do h_flip
            annotation["category_id"] = flip_parsing_instance_category(
                annotation["category_id"], transforms, parsing_flip_map
            )
        elif isinstance(segm, dict):
            # RLE
            mask = mask_util.decode(segm)
            mask = transforms.apply_segmentation(mask)
            assert tuple(mask.shape[:2]) == image_size
            annotation["segmentation"] = mask

            # change part label if do h_flip
            annotation["category_id"] = flip_parsing_instance_category(
                annotation["category_id"], transforms, cihp_flip_map
            )
        else:
            raise ValueError(
                "Cannot transform segmentation of type '{}'!"
                "Supported types are: polygons as list[list[float] or ndarray],"
                " COCO-style RLE as a dict.".format(type(segm))
            )

    return annotation


def flip_parsing_instance_category(category, transforms, flip_map):
    do_hflip = sum(isinstance(t, T.HFlipTransform) for t in transforms.transforms) % 2 == 1  # bool

    if do_hflip:
        for ori_label, new_label in flip_map:
            if category == ori_label:
                category = new_label
            elif category == new_label:
                category = ori_label
    return category


def compute_parsing_IoP(person_binary_mask, part_binary_mask):
    # both person_binary_mask and part_binary_mask are binary mask in shape (H, W)
    person = person_binary_mask.cpu()[:, :, None]
    person = mask_util.encode(np.array(person, order="F", dtype="uint8"))[0]
    person["counts"] = person["counts"].decode("utf-8")

    part = part_binary_mask.cpu()[:, :, None]
    part = mask_util.encode(np.array(part, order="F", dtype="uint8"))[0]
    part["counts"] = part["counts"].decode("utf-8")

    area_part = maskUtils.area(part)
    i = maskUtils.area(maskUtils.merge([person, part], True))
    return i / area_part


def affine_to_target_size(img, gt, target_size):
    assert img.shape[:2] == gt.shape
    org_h, org_w = img.shape[:2]
    bbox = np.asarray((0, 0, org_w, org_h))
    x0, y0, w, h = bbox
    xc = x0 + w * 0.5
    yc = y0 + h * 0.5

    aspect_ratio = target_size[0] / target_size[1]
    w, h = change_aspect_ratio(w, h, aspect_ratio)

    bbox = torch.tensor([xc, yc, w, h, 0.])
    trans = get_affine_transform(bbox, target_size)

    new_img = cv2.warpAffine(
        img, trans, (int(target_size[0]), int(target_size[1])), flags=cv2.INTER_LINEAR, borderValue=(128, 128, 128)
    )
    new_gt  = cv2.warpAffine(
        gt, trans, (int(target_size[0]), int(target_size[1])), flags=cv2.INTER_NEAREST, borderValue=(255, 255, 255)
    )
    return new_img, new_gt


def get_affine_transform(box, output_size, shift=np.array([0, 0], dtype=np.float32), inv=0):
    center = np.array([box[0], box[1]], dtype=np.float32)  # (xc, yc)
    scale = np.array([box[2], box[3]], dtype=np.float32)  # (w, h)
    rot = box[4]  # r

    src_w = scale[0]  # w
    dst_w = output_size[0]  # W
    dst_h = output_size[1]  # H

    rot_rad = np.pi * rot / 180  # r -> f(np.pi)
    src_dir = get_dir([0, src_w * -0.5], rot_rad)
    dst_dir = np.array([0, dst_w * -0.5], np.float32)

    src = np.zeros((3, 2), dtype=np.float32)
    dst = np.zeros((3, 2), dtype=np.float32)
    src[0, :] = center + scale * shift
    src[1, :] = center + src_dir + scale * shift
    dst[0, :] = [dst_w * 0.5, dst_h * 0.5]
    dst[1, :] = np.array([dst_w * 0.5, dst_h * 0.5]) + dst_dir

    src[2:, :] = get_3rd_point(src[0, :], src[1, :])
    dst[2:, :] = get_3rd_point(dst[0, :], dst[1, :])

    if inv:
        trans = cv2.getAffineTransform(np.float32(dst), np.float32(src))
    else:
        trans = cv2.getAffineTransform(np.float32(src), np.float32(dst))

    return trans


def get_3rd_point(a, b):
    direct = a - b
    return b + np.array([-direct[1], direct[0]], dtype=np.float32)


def get_dir(src_point, rot_rad):
    sn, cs = np.sin(rot_rad), np.cos(rot_rad)

    src_result = [0, 0]
    src_result[0] = src_point[0] * cs - src_point[1] * sn
    src_result[1] = src_point[0] * sn + src_point[1] * cs

    return src_result


def change_aspect_ratio(w, h, aspect_ratio):
    if w > aspect_ratio * h:
        h = w * 1.0 / aspect_ratio  # enlarge h
    elif w < aspect_ratio * h:
        w = h * aspect_ratio  # enlarge w
    return w, h


def center_to_target_size(img, gt, target_size):
    assert img.shape[:2] == gt.shape
    tfmd_h, tfmd_w = img.shape[0], img.shape[1]

    new_image = np.ones((target_size[1], target_size[0], 3), dtype=img.dtype) * 128
    new_gt = np.ones((target_size[1], target_size[0]), dtype=gt.dtype) * 255

    if tfmd_h > target_size[1] and tfmd_w > target_size[0]:
        range_ori_h = (int((tfmd_h - target_size[1]) / 2), int((tfmd_h + target_size[1]) / 2))
        range_ori_w = (int((tfmd_w - target_size[0]) / 2), int((tfmd_w + target_size[0]) / 2))

        new_image = img[range_ori_h[0]:range_ori_h[1], range_ori_w[0]:range_ori_w[1], :]
        new_gt = gt[range_ori_h[0]:range_ori_h[1], range_ori_w[0]:range_ori_w[1]]

    elif tfmd_h > target_size[1] and tfmd_w <= target_size[0]:
        range_ori_h = (int((tfmd_h - target_size[1]) / 2), int((tfmd_h + target_size[1]) / 2))
        range_new_w = (int((target_size[0] - tfmd_w) / 2), int((tfmd_w + target_size[0]) / 2))

        new_image[:, range_new_w[0]:range_new_w[1], :] = img[range_ori_h[0]:range_ori_h[1], :, :]
        new_gt[:, range_new_w[0]:range_new_w[1]] = gt[range_ori_h[0]:range_ori_h[1], :]

    elif tfmd_h <= target_size[1] and tfmd_w > target_size[0]:
        range_ori_w = (int((tfmd_w - target_size[0]) / 2), int((tfmd_w + target_size[0]) / 2))
        range_new_h = (int((target_size[1] - tfmd_h) / 2), int((tfmd_h + target_size[1]) / 2))

        new_image[range_new_h[0]:range_new_h[1], :, :] = img[:, range_ori_w[0]:range_ori_w[1], :]
        new_gt[range_new_h[0]:range_new_h[1], :] = gt[:, range_ori_w[0]:range_ori_w[1]]

    else:
        range_new_h = (int((target_size[1] - tfmd_h) / 2), int((tfmd_h + target_size[1]) / 2))
        range_new_w = (int((target_size[0] - tfmd_w) / 2), int((tfmd_w + target_size[0]) / 2))

        new_image[range_new_h[0]:range_new_h[1], range_new_w[0]:range_new_w[1], :] = img
        new_gt[range_new_h[0]:range_new_h[1], range_new_w[0]:range_new_w[1]] = gt

    return new_image, new_gt
