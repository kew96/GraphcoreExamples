# Copyright (c) 2020 Graphcore Ltd. All rights reserved.
# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
# Improved data throughput and added normalization interface
# Added a modification for fused normalization.
"""Provides utilities to preprocess images.

Training images are sampled using the provided bounding boxes, and subsequently
cropped to the sampled bounding box. Images are additionally flipped randomly,
then resized to the target output size (without aspect-ratio preservation).

Images used during evaluation are resized (with aspect-ratio preservation) and
centrally cropped.

All images undergo mean color subtraction.

Note that these steps are colloquially referred to as "ResNet preprocessing,"
and they differ from "VGG preprocessing," which does not use bounding boxes
and instead does an aspect-preserving resize followed by random crop during
training. (These both differ from "Inception preprocessing," which introduces
color distortion steps.)

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
from tensorflow.python import ipu

# FROM https://github.com/tensorflow/models/blob/master/official/vision/image_classification/resnet/imagenet_preprocessing.py

NUM_CLASSES = 1000

NUM_IMAGES = 1200000  # 1.2M
NUM_VALIDATION_IMAGES = 50000  # 50k

_NUM_TRAIN_FILES = 1024
# values taken from https://github.com/pytorch/examples/blob/master/imagenet/main.py#L198
NORMALIZED_MEAN = [0.485, 0.456, 0.406]
NORMALIZED_STD = [0.229, 0.224, 0.225]
ORIGINAL_MEAN = [123.68, 116.78, 103.94]


def _parse_example_proto(example_serialized):
    """Parses an Example proto containing a training example of an image.
    The output of the build_image_data.py image preprocessing script is a dataset
    containing serialized Example protocol buffers. Each Example proto contains
    the following fields (values are included as examples):
      image/height: 462
      image/width: 581
      image/colorspace: 'RGB'
      image/channels: 3
      image/class/label: 615
      image/class/synset: 'n03623198'
      image/class/text: 'knee pad'
      image/object/bbox/xmin: 0.1
      image/object/bbox/xmax: 0.9
      image/object/bbox/ymin: 0.2
      image/object/bbox/ymax: 0.6
      image/object/bbox/label: 615
      image/format: 'JPEG'
      image/filename: 'ILSVRC2012_val_00041207.JPEG'
      image/encoded: <JPEG encoded string>
    Args:
      example_serialized: scalar Tensor tf.string containing a serialized
        Example protocol buffer.
    Returns:
      image_buffer: Tensor tf.string containing the contents of a JPEG file.
      label: Tensor tf.int32 containing the label.
      bbox: 3-D float Tensor of bounding boxes arranged [1, num_boxes, coords]
        where each coordinate is [0, 1) and the coordinates are arranged as
        [ymin, xmin, ymax, xmax].
    """
    # Dense features in Example proto.
    feature_map = {
        'image/encoded': tf.io.FixedLenFeature([], dtype=tf.string,
                                               default_value=''),
        'image/class/label': tf.io.FixedLenFeature([], dtype=tf.int64,
                                                   default_value=-1),
        'image/class/text': tf.io.FixedLenFeature([], dtype=tf.string,
                                                  default_value=''),
    }
    sparse_float32 = tf.io.VarLenFeature(dtype=tf.float32)
    # Sparse features in Example proto.
    feature_map.update(
        {k: sparse_float32 for k in ['image/object/bbox/xmin',
                                     'image/object/bbox/ymin',
                                     'image/object/bbox/xmax',
                                     'image/object/bbox/ymax']})

    features = tf.io.parse_single_example(example_serialized, feature_map)
    label = tf.cast(features['image/class/label'], dtype=tf.int32)

    xmin = tf.expand_dims(features['image/object/bbox/xmin'].values, 0)
    ymin = tf.expand_dims(features['image/object/bbox/ymin'].values, 0)
    xmax = tf.expand_dims(features['image/object/bbox/xmax'].values, 0)
    ymax = tf.expand_dims(features['image/object/bbox/ymax'].values, 0)

    # Note that we impose an ordering of (y, x) just to make life difficult.
    bbox = tf.concat([ymin, xmin, ymax, xmax], 0)

    # Force the variable number of bounding boxes into the shape
    # [1, num_boxes, coords].
    bbox = tf.expand_dims(bbox, 0)
    bbox = tf.transpose(bbox, [0, 2, 1])

    return features['image/encoded'], label, bbox


def parse_record(raw_record, is_training, dtype, image_size, full_normalisation, seed=None):
    """Parses a record containing a training example of an image.
    The input record is parsed into a label and image, and the image is passed
    through preprocessing steps (cropping, flipping, and so on).
    Args:
      raw_record: scalar Tensor tf.string containing a serialized
        Example protocol buffer.
      is_training: A boolean denoting whether the input is for training.
      dtype: data type to use for images/features.
    Returns:
      Tuple with processed image tensor and one-hot-encoded label tensor.
    """
    image_buffer, label, bbox = _parse_example_proto(raw_record)

    image = preprocess_image(
        image_buffer=image_buffer,
        bbox=bbox,
        output_height=image_size,
        output_width=image_size,
        num_channels=3,
        is_training=is_training,
        full_normalisation=full_normalisation,
        seed=seed)
    image = tf.cast(image, dtype)
    return image, label


def _decode_crop_and_flip(image_buffer, bbox, num_channels, seed=None):
    """Crops the given image to a random part of the image, and randomly flips.

    We use the fused decode_and_crop op, which performs better than the two ops
    used separately in series, but note that this requires that the image be
    passed in as an un-decoded string Tensor.

    Args:
      image_buffer: scalar string Tensor representing the raw JPEG image buffer.
      bbox: 3-D float Tensor of bounding boxes arranged [1, num_boxes, coords]
        where each coordinate is [0, 1) and the coordinates are arranged as
        [ymin, xmin, ymax, xmax].
      num_channels: Integer depth of the image buffer for decoding.

    Returns:
      3-D tensor with cropped image.

    """
    # A large fraction of image datasets contain a human-annotated bounding box
    # delineating the region of the image containing the object of interest.  We
    # choose to create a new bounding box for the object which is a randomly
    # distorted version of the human-annotated bounding box that obeys an
    # allowed range of aspect ratios, sizes and overlap with the human-annotated
    # bounding box. If no box is supplied, then we assume the bounding box is
    # the entire image.
    sample_distorted_bounding_box = tf.image.sample_distorted_bounding_box(
        tf.image.extract_jpeg_shape(image_buffer),
        bounding_boxes=bbox,
        min_object_covered=0.1,
        aspect_ratio_range=[0.75, 1.33],
        area_range=[0.05, 1.0],
        max_attempts=100,
        use_image_if_no_bounding_boxes=True,
        seed=seed)
    bbox_begin, bbox_size, _ = sample_distorted_bounding_box

    # Reassemble the bounding box in the format the crop op requires.
    offset_y, offset_x, _ = tf.unstack(bbox_begin)
    target_height, target_width, _ = tf.unstack(bbox_size)
    crop_window = tf.stack([offset_y, offset_x, target_height, target_width])

    # Use the fused decode and crop op here, which is faster than each in series.
    cropped = tf.image.decode_and_crop_jpeg(
        image_buffer, crop_window, channels=num_channels, dct_method='INTEGER_FAST')

    # Flip to add a little more random distortion in.
    cropped = tf.image.random_flip_left_right(cropped, seed=seed)
    return cropped


def _central_crop(image, crop_height, crop_width):
    """Performs central crops of the given image list.

    Args:
      image: a 3-D image tensor
      crop_height: the height of the image following the crop.
      crop_width: the width of the image following the crop.

    Returns:
      3-D tensor with cropped image.
    """
    shape = tf.shape(image)
    height, width = shape[0], shape[1]

    amount_to_be_cropped_h = (height - crop_height)
    crop_top = amount_to_be_cropped_h // 2
    amount_to_be_cropped_w = (width - crop_width)
    crop_left = amount_to_be_cropped_w // 2
    return tf.slice(
        image, [crop_top, crop_left, 0], [crop_height, crop_width, -1])


def _mean_image_subtraction(image, means, num_channels):
    """Subtracts the given means from each image channel.

    For example:
      means = [123.68, 116.779, 103.939]
      image = _mean_image_subtraction(image, means)

    Note that the rank of `image` must be known.

    Args:
      image: a tensor of size [height, width, C].
      means: a C-vector of values to subtract from each channel.
      num_channels: number of color channels in the image that will be distorted.

    Returns:
      the centered image.

    Raises:
      ValueError: If the rank of `image` is unknown, if `image` has a rank other
        than three or if the number of channels in `image` doesn't match the
        number of values in `means`.
    """
    if image.get_shape().ndims not in (3, 4):
        raise ValueError('Input must be of size [height, width, C>0] or [batch, height, width, C>0]')

    if len(means) != num_channels:
        raise ValueError('len(means) must match the number of channels')

    # We have a 1-D tensor of means; convert to 3-D.
    means = tf.expand_dims(tf.expand_dims(means, 0), 0)

    if image.get_shape().ndims == 4:
        means = tf.expand_dims(means, 0)

    return image - tf.cast(means, image.dtype)


def _normalise_image(image, means, std_dev, num_channels):
    """Normalise each image channel.
    """
    if image.get_shape().ndims not in (3, 4):
        raise ValueError('Input must be of size [height, width, C>0]')

    if len(means) != num_channels:
        raise ValueError('len(means) must match the number of channels')

    means = tf.cast(means, dtype=image.dtype)
    std_dev = tf.cast(std_dev, dtype=image.dtype)
    # We have a 1-D tensor of means; convert to 3-D.
    means = tf.expand_dims(tf.expand_dims(means, 0), 0)
    std_dev = tf.expand_dims(tf.expand_dims(std_dev, 0), 0)

    if image.get_shape().ndims == 4:
        means = tf.expand_dims(means, 0)
        std_dev = tf.expand_dims(std_dev, 0)

    image /= 255.0
    image -= means
    image /= std_dev

    return image


def _smallest_size_at_least(height, width, resize_min):
    """Computes new shape with the smallest side equal to `smallest_side`.

    Computes new shape with the smallest side equal to `smallest_side` while
    preserving the original aspect ratio.

    Args:
      height: an int32 scalar tensor indicating the current height.
      width: an int32 scalar tensor indicating the current width.
      resize_min: A python integer or scalar `Tensor` indicating the size of
        the smallest side after resize.

    Returns:
      new_height: an int32 scalar tensor indicating the new height.
      new_width: an int32 scalar tensor indicating the new width.
    """
    resize_min = tf.cast(resize_min, tf.float32)

    # Convert to floats to make subsequent calculations go smoothly.
    height, width = tf.cast(height, tf.float32), tf.cast(width, tf.float32)

    smaller_dim = tf.minimum(height, width)
    scale_ratio = resize_min / smaller_dim

    # Convert back to ints to make heights and widths that TF ops will accept.
    new_height = tf.cast(height * scale_ratio, tf.int32)
    new_width = tf.cast(width * scale_ratio, tf.int32)

    return new_height, new_width


def _aspect_preserving_resize(image, resize_min):
    """Resize images preserving the original aspect ratio.

    Args:
      image: A 3-D image `Tensor`.
      resize_min: A python integer or scalar `Tensor` indicating the size of
        the smallest side after resize.

    Returns:
      resized_image: A 3-D tensor containing the resized image.
    """
    shape = tf.shape(image)
    height, width = shape[0], shape[1]

    new_height, new_width = _smallest_size_at_least(height, width, resize_min)

    return _resize_image(image, new_height, new_width)


def _resize_image(image, height, width):
    """Simple wrapper around tf.resize_images.

    This is primarily to make sure we use the same `ResizeMethod` and other
    details each time.

    Args:
      image: A 3-D image `Tensor`.
      height: The target height for the resized image.
      width: The target width for the resized image.

    Returns:
      resized_image: A 3-D tensor containing the resized image. The first two
        dimensions have the shape [height, width].
    """
    return tf.image.resize_images(
        image, [height, width], method=tf.image.ResizeMethod.BILINEAR,
        align_corners=False)


def normalise_image(image, full_normalisation=False, num_channels=3):
    if full_normalisation:
        image = _normalise_image(image, NORMALIZED_MEAN, NORMALIZED_STD, num_channels)
    else:
        image = _mean_image_subtraction(image, ORIGINAL_MEAN, num_channels)
    return image


def fused_normalise_image(image, full_normalisation=False):
    """Call fused operation for image normalisation.
    """
    scale = 1.0/255.0 if full_normalisation else 1.0
    mean = NORMALIZED_MEAN if full_normalisation else ORIGINAL_MEAN
    std = NORMALIZED_STD if full_normalisation else [1.0, 1.0, 1.0]
    return ipu.image_ops.normalise_image(image, mean, std, scale)


def preprocess_image(image_buffer, bbox, output_height, output_width,
                     num_channels, is_training=False, full_normalisation=False, seed=None):
    """Preprocesses the given image.

    Preprocessing includes decoding, cropping, and resizing for both training
    and eval images. Training preprocessing, however, introduces some random
    distortion of the image to improve accuracy.

    Args:
      image_buffer: scalar string Tensor representing the raw JPEG image buffer.
      bbox: 3-D float Tensor of bounding boxes arranged [1, num_boxes, coords]
        where each coordinate is [0, 1) and the coordinates are arranged as
        [ymin, xmin, ymax, xmax].
      output_height: The height of the image after preprocessing.
      output_width: The width of the image after preprocessing.
      num_channels: Integer depth of the image buffer for decoding.
      is_training: `True` if we're preprocessing the image for training and
        `False` otherwise.

    Returns:
      A preprocessed image.
    """
    if is_training:
        # For training, we want to randomize some of the distortions.
        image = _decode_crop_and_flip(image_buffer, bbox, num_channels, seed)
        image = _resize_image(image, output_height, output_width)
    else:
        # For validation, we want to decode, resize, then just crop the middle.
        image = tf.image.decode_jpeg(image_buffer, channels=num_channels)
        # The lower bound for the smallest side of the image for aspect-preserving
        # resizing. Originally set to 256 for 224x224 image sizes. Now scaled by the
        # prescribed image size.
        _RESIZE_MIN = int(output_height * float(256) / float(224))
        image = _aspect_preserving_resize(image, _RESIZE_MIN)
        image = _central_crop(image, output_height, output_width)

    image.set_shape([output_height, output_width, num_channels])

    if full_normalisation is None:
        print("\nImage normalisation will be performed on the IPU.\n")
    else:
        image = normalise_image(image, full_normalisation=full_normalisation, num_channels=num_channels)
    return image
