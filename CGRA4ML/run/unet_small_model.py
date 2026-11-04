import numpy as np
import tensorflow as tf
from tensorflow import keras

from deepsocflow import SYS_BITS, XActivation, XBundle, XConvBN, XModel, XPool


SMALL_UNET_PROFILES = {
    "default": ((12, 12, 2), ((2, 3, 1), (3, 4, 0), (4, 2, 1),
                               (6, 2, 0), (5, 2, 0))),
    "grouped": ((12, 8, 9), ((9, 9, 1), (9, 4, 0), (4, 2, 1),
                              (6, 2, 0), (11, 2, 0))),
}


def lower_depthwise_kernel(kernel):
    """Map [KH, KW, CI, multiplier] weights to an ordinary Conv2D kernel.

    Output c * multiplier + m reads only input c. Preserve bias, padding,
    stride and intervening quantizers separately. Accept real weights or
    zero-point-zero integers, not affine codes. This is inference conversion.
    """
    kernel = np.asarray(kernel)
    if kernel.ndim != 4 or any(size <= 0 for size in kernel.shape):
        raise ValueError("depthwise kernel must have positive KH, KW, CI, multiplier")
    if (not (np.issubdtype(kernel.dtype, np.integer) or
             np.issubdtype(kernel.dtype, np.floating)) or
            not np.isfinite(kernel).all()):
        raise ValueError("depthwise kernel must contain finite numeric weights")
    kh, kw, channels, multiplier = kernel.shape
    dense = np.zeros((kh, kw, channels, channels * multiplier), dtype=kernel.dtype)
    for channel in range(channels):
        dense[:, :, channel, channel * multiplier:(channel + 1) * multiplier] = kernel[:, :, channel, :]
    return dense


class SmallUNet(XModel):
    """Five depthwise/pointwise pairs, two pools, two upsample/concat stages."""
    def __init__(self, channels=None):
        sys_bits = SYS_BITS(x=4, k=4, b=16)
        super().__init__(sys_bits, x_int_bits=0)
        if channels is None:
            channels = SMALL_UNET_PROFILES["default"][1]

        def activation(integer=0):
            return XActivation(sys_bits=sys_bits, o_int_bits=integer, type=None)

        self.depthwise = []
        self.pointwise = []
        for index, (ci, co, integer) in enumerate(channels):
            join = dict(concat_act=activation(index == 4), skip_first=index == 4) if index >= 3 else {}
            self.depthwise.append(XBundle(core=XConvBN(
                k_int_bits=0, b_int_bits=0, filters=ci, kernel_size=3, epsilon=0.0,
                act=activation(integer)), **join))
            post = {}
            if index in (1, 2):
                post['pool'] = XPool(type='max', pool_size=(2, 2), strides=(2, 2),
                                     padding='valid', act=activation(integer))
            if index in (2, 3):
                post['upsample'] = (2, 2)
            self.pointwise.append(XBundle(core=XConvBN(
                k_int_bits=0, b_int_bits=0, filters=co, kernel_size=1, epsilon=0.0,
                act=activation(integer)), **post))

    def call(self, x):
        high = self.pointwise[0](self.depthwise[0](self.input_quant_layer(x)))
        low = self.pointwise[1](self.depthwise[1](high))
        x = self.pointwise[2](self.depthwise[2](low))
        x = self.pointwise[3](self.depthwise[3](x, x_concat=low))
        return self.pointwise[4](self.depthwise[4](x, x_concat=high))


def build_small_unet(seed=1234, profile="default"):
    """Return model, fixture and native depthwise kernels for reference checks."""
    if profile not in SMALL_UNET_PROFILES:
        raise ValueError(f"unknown small U-Net profile: {profile}")
    shape, channels = SMALL_UNET_PROFILES[profile]
    keras.utils.set_random_seed(seed)
    fixture = SmallUNet(channels)
    inputs = keras.Input(shape)
    model = keras.Model(inputs, fixture(inputs))
    rng = np.random.default_rng(seed)
    kernels = []
    for dw, pw in zip(fixture.depthwise, fixture.pointwise):
        channels = int(dw.core.kernel.shape[2])
        kernel = rng.integers(-7, 8, (3, 3, channels, 1)).astype(np.float32) / 8
        dw.core.kernel.assign(lower_depthwise_kernel(kernel))
        kernels.append(kernel)
        # Identity BN preserves the explicitly quantized inference weights.
        for bundle in (dw, pw):
            bundle.core.bias.assign(tf.zeros_like(bundle.core.bias))
        pw.core.kernel.assign(rng.integers(-7, 8, pw.core.kernel.shape).astype(np.float32) / 8)
    return model, fixture, kernels
