'''
Build a simple neural language model using GRU units
'''

import numpy as np
import os
import theano
import theano.tensor as T
import theano.tensor as tensor
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
from t8_data import Text8

import cPickle as pkl
import ipdb
import numpy
import copy

import warnings
import time

from collections import OrderedDict

#from char_data_iterator import TextIterator

profile = False
seed = 1234
numpy.random.seed(seed)


def param_init_hsoftmax(options, params, nin, ncls, nout, prefix='hsoftmax'):
    nout_per_cls = (nout + ncls - 1) / ncls
    W1 = numpy.asarray(numpy.random.normal(
        0, 0.01, size=(nin, ncls)), dtype=theano.config.floatX)
    b1 = numpy.asarray(numpy.zeros((ncls,)), dtype=theano.config.floatX)

    # Second level of h_softmax
    W2 = numpy.asarray(numpy.random.normal(
        0, 0.01, size=(ncls, nin, nout_per_cls)), dtype=theano.config.floatX)
    b2 = numpy.asarray(numpy.zeros((ncls, nout_per_cls)), dtype=theano.config.floatX)

    # store some private vars
    options['hsoftmax_ncls'] = ncls
    options['nvocab'] = nout
    params[_p(prefix, 'W1')] = W1
    params[_p(prefix, 'W2')] = W2
    params[_p(prefix, 'b1')] = b1
    params[_p(prefix, 'b2')] = b2
    return params


def hsoftmax_layer(tparams, state_below, options, y_indexes=None,
                   prefix='hsoftmax', compute_all=False, **kwargs):
    """
    shape of state_below is expected to be: (#tsteps, #batchsize, #dim)
    y_indexes: a theano variable of true targets.
    """
    ncls = options['hsoftmax_ncls']
    nout = options['nvocab']
    nout_per_cls = (nout + ncls - 1) / ncls
    state_shp = state_below.shape

    if state_below.ndim == 3:
        reshaped = 1
        state_reshp = state_below.reshape([state_shp[0] * state_shp[1], state_shp[2]])
        batch_size = state_shp[1] * state_shp[0]
    else:
        reshaped = 0
        state_reshp = state_below
        batch_size = state_shp[0]

    if compute_all:
        # shape: (batch_size, output_size)  (batch size after reshaping)
        output = tensor.nnet.h_softmax(state_reshp, batch_size, nout,
                                       ncls, nout_per_cls,
                                       tparams[_p(prefix, 'W1')],
                                       tparams[_p(prefix, 'b1')],
                                       tparams[_p(prefix, 'W2')],
                                       tparams[_p(prefix, 'b2')])
        if reshaped:
            output = output.reshape([state_shp[0], state_shp[1], -1])
    else:
        if y_indexes != None:
            y_indexes = y_indexes.flatten()
        # shape: (batch_size,)
        output = tensor.nnet.h_softmax(state_reshp, batch_size, nout,
                                       ncls, nout_per_cls,
                                       tparams[_p(prefix, 'W1')],
                                       tparams[_p(prefix, 'b1')],
                                       tparams[_p(prefix, 'W2')],
                                       tparams[_p(prefix, 'b2')], y_indexes)
        if reshaped:
            output = output.reshape([state_shp[0], state_shp[1]])
    return output


def masked_softmax(x, axis=-1, mask=None):
    if mask is not None:
        x = (mask * x) + (1 - mask) * (-10)
        x = tensor.clip(x, -10., 10.)
    e_x = tensor.exp(x - tensor.max(x, axis=axis, keepdims=True))
    if mask is not None:
        e_x = e_x * mask
    softmax = e_x / (tensor.sum(e_x, axis=axis, keepdims=True) + 1e-6)
    return softmax


def gradient_clipping(grads, tparams, clip_c=1.0):
    g2 = 0.
    for g in grads:
        g2 += (g**2).sum()
    g2 = tensor.sqrt(g2)
    not_finite = tensor.or_(tensor.isnan(g2), tensor.isinf(g2))
    new_grads = []
    lr = tensor.scalar(name='lr')
    for p, g in zip(tparams.values(), grads):
        new_grads.append(tensor.switch(
            g2 > clip_c, g * (clip_c / g2), g))
    return new_grads, not_finite, tensor.lt(clip_c, g2)


def categorical_crossentropy(target, output):
    '''
    Compute categorical cross-entropy between targets and model output.
    '''
    assert (target.ndim == 2)
    assert (output.ndim == 3)
    output = output.reshape((output.shape[0] * output.shape[1], output.shape[2]))
    t_flat = target.flatten()
    probs = tensor.diag(output.T[t_flat])
    probs = probs.reshape((target.shape[0], target.shape[1]))
    return -tensor.log(probs + 1e-6)


# push parameters to Theano shared variables
def zipp(params, tparams):
    for kk, vv in params.iteritems():
        tparams[kk].set_value(vv)


# pull parameters from Theano shared variables
def unzip(zipped):
    new_params = OrderedDict()
    for kk, vv in zipped.iteritems():
        new_params[kk] = vv.get_value()
    return new_params


def chunk(sequence, n):
    """ Yield successive n-sized chunks from sequence. """
    for i in range(0, len(sequence), n):
        yield sequence[i:i + n]

C = - 0.5 * np.log(2 * np.pi)

def log_prob_gaussian(x, mean, log_var):
    return C - log_var / 2 - (x - mean) ** 2 / (2 * T.exp(log_var))


def gaussian_kld(mu_left, logvar_left, mu_right, logvar_right):
    gauss_klds = 0.5 * (logvar_right - logvar_left + (tensor.exp(logvar_left) / tensor.exp(logvar_right)) + ((mu_left - mu_right)**2.0 / tensor.exp(logvar_right)) - 1.0)
    return gauss_klds


# get the list of parameters: Note that tparams must be OrderedDict
def itemlist(tparams):
    return [vv for kk, vv in tparams.iteritems()]


# dropout
def dropout_layer(state_before, use_noise, trng):
    proj = tensor.switch(
        use_noise,
        state_before * trng.binomial(state_before.shape, p=0.5, n=1,
                                     dtype=state_before.dtype),
        state_before * 0.5)
    return proj


# make prefix-appended name
def _p(pp, name):
    return '%s_%s' % (pp, name)


# initialize Theano shared variables according to the initial parameters
def init_tparams(params):
    tparams = OrderedDict()
    for kk, pp in params.iteritems():
        tparams[kk] = theano.shared(params[kk], name=kk)
    return tparams


# load parameters
def load_params(path, params):
    pp = numpy.load(path)
    for kk, vv in params.iteritems():
        if kk not in pp:
            warnings.warn('%s is not in the archive' % kk)
            continue
        params[kk] = pp[kk]

    return params


# layers: 'name': ('parameter initializer', 'feedforward')
layers = {
    'ff': ('param_init_fflayer', 'fflayer'),
    'gru': ('param_init_gru', 'gru_layer'),
    'lstm': ('param_init_lstm', 'lstm_layer'),
    'hsoftmax': ('param_init_hsoftmax', 'hsoftmax_layer'),
    'latent_lstm': ('param_init_lstm', 'latent_lstm_layer'),
}


def get_layer(name):
    fns = layers[name]
    return (eval(fns[0]), eval(fns[1]))


# orthogonal initialization for weights
# see Saxe et al. ICLR'14
def ortho_weight(ndim):
    W = numpy.random.randn(ndim, ndim)
    u, s, v = numpy.linalg.svd(W)
    return u.astype('float32')


# weight initializer, normal by default
def norm_weight(nin, nout=None, scale=0.01, ortho=True):
    if nout is None:
        nout = nin
    if nout == nin and ortho:
        W = ortho_weight(nin)
    else:
        W = scale * numpy.random.randn(nin, nout)
    return W.astype('float32')


def lrelu(x):
    return tensor.clip(tensor.nnet.relu(x, 1. / 3), -3.0, 3.0)


def tanh(x):
    return tensor.tanh(x)


def linear(x):
    return x


def concatenate(tensor_list, axis=0):
    """
    Alternative implementation of `theano.tensor.concatenate`.
    This function does exactly the same thing, but contrary to Theano's own
    implementation, the gradient is implemented on the GPU.
    Backpropagating through `theano.tensor.concatenate` yields slowdowns
    because the inverse operation (splitting) needs to be done on the CPU.
    This implementation does not have that problem.
    :usage:
        >>> x, y = theano.tensor.matrices('x', 'y')
        >>> c = concatenate([x, y], axis=1)
    :parameters:
        - tensor_list : list
            list of Theano tensor expressions that should be concatenated.
        - axis : int
            the tensors will be joined along this axis.
    :returns:
        - out : tensor
            the concatenated tensor expression.
    """
    concat_size = sum(tt.shape[axis] for tt in tensor_list)

    output_shape = ()
    for k in range(axis):
        output_shape += (tensor_list[0].shape[k],)
    output_shape += (concat_size,)
    for k in range(axis + 1, tensor_list[0].ndim):
        output_shape += (tensor_list[0].shape[k],)

    out = tensor.zeros(output_shape)
    offset = 0
    for tt in tensor_list:
        indices = ()
        for k in range(axis):
            indices += (slice(None),)
        indices += (slice(offset, offset + tt.shape[axis]),)
        for k in range(axis + 1, tensor_list[0].ndim):
            indices += (slice(None),)

        out = tensor.set_subtensor(out[indices], tt)
        offset += tt.shape[axis]

    return out


# feedforward layer: affine transformation + point-wise nonlinearity
def param_init_fflayer(options, params, prefix='ff', nin=None, nout=None,
                       ortho=True):
    if nin is None:
        nin = options['dim_proj']
    if nout is None:
        nout = options['dim_proj']
    params[_p(prefix, 'W')] = norm_weight(nin, nout, scale=0.01, ortho=ortho)
    params[_p(prefix, 'b')] = numpy.zeros((nout,)).astype('float32')

    return params


def fflayer(tparams, state_below, options, prefix='rconv',
            activ='lambda x: tensor.tanh(x)', **kwargs):
    if state_below.dtype == 'int32' or state_below.dtype == 'int64':
        return tparams[_p(prefix, 'W')][state_below] + tparams[_p(prefix, 'b')]
    return eval(activ)(
        tensor.dot(state_below, tparams[_p(prefix, 'W')]) +
        tparams[_p(prefix, 'b')])


def param_init_lstm(options,
                     params,
                     prefix='lstm',
                     nin=None,
                     dim=None):
     if nin is None:
         nin = options['dim_proj']

     if dim is None:
         dim = options['dim_proj']

     W = numpy.concatenate([norm_weight(nin,dim),
                            norm_weight(nin,dim),
                            norm_weight(nin,dim),
                            norm_weight(nin,dim)],
                            axis=1)

     params[_p(prefix,'W')] = W
     U = numpy.concatenate([ortho_weight(dim),
                            ortho_weight(dim),
                            ortho_weight(dim),
                            ortho_weight(dim)],
                            axis=1)

     params[_p(prefix,'U')] = U
     params[_p(prefix,'b')] = numpy.zeros((4 * dim,)).astype('float32')

     return params

def lstm_layer(tparams, state_below,
                options,
                prefix='lstm',
                mask=None, one_step=False,
                init_state=None,
                init_memory=None,
                nsteps=None,
                **kwargs):

     if nsteps is None:
         nsteps = state_below.shape[0]

     if state_below.ndim == 3:
         n_samples = state_below.shape[1]
     else:
         n_samples = 1

     param = lambda name: tparams[_p(prefix, name)]
     dim = param('U').shape[0]

     if mask is None:
         mask = tensor.alloc(1., state_below.shape[0], 1)

     # initial/previous state
     if init_state is None:
         if not options['learn_h0']:
             init_state = tensor.alloc(0., n_samples, dim)
         else:
             init_state0 = theano.shared(numpy.zeros((options['dim'])),
                                  name=_p(prefix, "h0"))
             init_state = tensor.alloc(init_state0, n_samples, dim)
             tparams[_p(prefix, 'h0')] = init_state0

     U = param('U')
     b = param('b')
     W = param('W')
     non_seqs = [U, b, W]

     # initial/previous memory
     if init_memory is None:
         init_memory = tensor.alloc(0., n_samples, dim)

     def _slice(_x, n, dim):
         if _x.ndim == 3:
             return _x[:, :, n*dim:(n+1)*dim]
         return _x[:, n*dim:(n+1)*dim]

     def _step(mask, sbelow, sbefore, cell_before, *args):
         preact = tensor.dot(sbefore, param('U'))
         preact += sbelow
         preact += param('b')

         i = tensor.nnet.sigmoid(_slice(preact, 0, dim))
         f = tensor.nnet.sigmoid(_slice(preact, 1, dim))
         o = tensor.nnet.sigmoid(_slice(preact, 2, dim))
         c = tensor.tanh(_slice(preact, 3, dim))

         c = f * cell_before + i * c
         c = mask * c + (1. - mask) * cell_before
         h = o * tensor.tanh(c)
         h = mask * h + (1. - mask) * sbefore

         return h, c

     lstm_state_below = tensor.dot(state_below, param('W')) + param('b')
     if state_below.ndim == 3:
         lstm_state_below = lstm_state_below.reshape((state_below.shape[0],
                                                      state_below.shape[1],
                                                      -1))
     if one_step:
         mask = mask.dimshuffle(0, 'x')
         h, c = _step(mask, lstm_state_below, init_state, init_memory)
         rval = [h, c]
     else:
         if mask.ndim == 3 and mask.ndim == state_below.ndim:
             mask = mask.reshape((mask.shape[0], \
                                  mask.shape[1]*mask.shape[2])).dimshuffle(0, 1, 'x')
         elif mask.ndim == 2:
             mask = mask.dimshuffle(0, 1, 'x')

         rval, updates = theano.scan(_step,
                                     sequences=[mask, lstm_state_below],
                                     outputs_info=[init_state, init_memory],
                                     name=_p(prefix, '_layers'),
                                     non_sequences=non_seqs,
                                     strict=True,
                                     n_steps=nsteps)
     return [rval, updates]


def latent_lstm_layer(
        tparams, state_below,
        options, prefix='lstm', back_states = None,
        gaussian_s=None, mask=None, one_step=False,
        init_state=None, init_memory=None, nsteps=None,
        **kwargs):

    if nsteps is None:
        nsteps = state_below.shape[0]

    if state_below.ndim == 3:
        n_samples = state_below.shape[1]
    else:
        n_samples = 1

    param = lambda name: tparams[_p(prefix, name)]
    dim = param('U').shape[0]

    if mask is None:
        mask = tensor.alloc(1., state_below.shape[0], 1)

    # initial/previous state
    if init_state is None:
        if not options['learn_h0']:
            init_state = tensor.alloc(0., n_samples, dim)
        else:
            init_state0 = theano.shared(numpy.zeros((options['dim'])),
                                        name=_p(prefix, "h0"))
            init_state = tensor.alloc(init_state0, n_samples, dim)
            tparams[_p(prefix, 'h0')] = init_state0

    U = param('U')
    b = param('b')
    W = param('W')
    non_seqs = [U, b, W, tparams[_p('z_cond', 'W')],
                tparams[_p('trans_1', 'W')],
                tparams[_p('trans_1', 'b')],
                tparams[_p('z_mus', 'W')],
                tparams[_p('z_mus', 'b')],
                tparams[_p('inf', 'W')],
                tparams[_p('inf', 'b')],
                tparams[_p('inf_mus', 'W')],
                tparams[_p('inf_mus', 'b')],
                tparams[_p('gen_mus', 'W')],
                tparams[_p('gen_mus', 'b')]]

    # initial/previous memory
    if init_memory is None:
        init_memory = tensor.alloc(0., n_samples, dim)

    def _slice(_x, n, dim):
        if _x.ndim == 3:
            return _x[:, :, n*dim:(n+1)*dim]
        return _x[:, n*dim:(n+1)*dim]

    def _step(mask, sbelow, d_, g_s, sbefore, cell_before,
              U, b, W, W_cond, trans_1_w, trans_1_b,
              z_mus_w, z_mus_b,
              inf_w, inf_b,
              inf_mus_w, inf_mus_b,
              gen_mus_w, gen_mus_b):

        p_z = tensor.nnet.softplus(tensor.dot(sbefore, trans_1_w) + trans_1_b)
        z_mus = tensor.dot(p_z, z_mus_w) + z_mus_b
        z_dim = z_mus.shape[-1] / 2
        z_mu, z_sigma = z_mus[:, :z_dim], z_mus[:, z_dim:]

        if d_ is not None:
            encoder_hidden = tensor.nnet.softplus(tensor.dot(concatenate([sbefore, d_], axis=1), inf_w) + inf_b)
            encoder_mus = tensor.dot(encoder_hidden, inf_mus_w) + inf_mus_b
            encoder_mu, encoder_sigma = encoder_mus[:, :z_dim], encoder_mus[:, z_dim:]
            tild_z_t = encoder_mu + g_s * tensor.exp(0.5 * encoder_sigma)
            kld = gaussian_kld(encoder_mu, encoder_sigma, z_mu, z_sigma)
            kld = tensor.sum(kld, axis=-1)
            decoder_mus = tensor.dot(tild_z_t, gen_mus_w) + gen_mus_b
            decoder_mu, decoder_sigma = decoder_mus[:, :d_.shape[1]], decoder_mus[:, d_.shape[1]:]
            decoder_mu = tensor.tanh(decoder_mu)
            decoder_mu = T.clip(decoder_mu, -10., 10.)
            decoder_sigma = T.clip(decoder_sigma, -10., 10.)
            disc_d_ = theano.gradient.disconnected_grad(d_)
            recon_cost = tensor.sqr(decoder_mu - disc_d_)
            recon_cost = tensor.sum(recon_cost, axis=-1)
        else:
            tild_z_t = z_mu + g_s * tensor.exp(0.5 * z_sigma)
            kld = tensor.sum(tild_z_t, axis=-1) * 0.
            recon_cost = tensor.sum(tild_z_t, axis=-1) * 0.

        z = tild_z_t
        preact = tensor.dot(sbefore, param('U')) +  tensor.dot(z, W_cond)
        preact += sbelow
        preact += param('b')

        i = tensor.nnet.sigmoid(_slice(preact, 0, dim))
        f = tensor.nnet.sigmoid(_slice(preact, 1, dim))
        o = tensor.nnet.sigmoid(_slice(preact, 2, dim))
        c = tensor.tanh(_slice(preact, 3, dim))

        c = f * cell_before + i * c
        c = mask * c + (1. - mask) * cell_before
        h = o * tensor.tanh(c)
        h = mask * h + (1. - mask) * sbefore
        return h, c, z, kld, recon_cost

    lstm_state_below = tensor.dot(state_below, param('W')) + param('b')
    if state_below.ndim == 3:
        lstm_state_below = lstm_state_below.reshape((state_below.shape[0],
                                                     state_below.shape[1],
                                                     -1))
    if one_step:
        mask = mask.dimshuffle(0, 'x')
        h, c = _step(mask, lstm_state_below, init_state, init_memory)
        rval = [h, c]
    else:
        if mask.ndim == 3 and mask.ndim == state_below.ndim:
            mask = mask.reshape((mask.shape[0], \
                                 mask.shape[1]*mask.shape[2])).dimshuffle(0, 1, 'x')
        elif mask.ndim == 2:
            mask = mask.dimshuffle(0, 1, 'x')

        rval, updates = theano.scan(
            _step, sequences=[mask, lstm_state_below, back_states, gaussian_s],
            outputs_info = [init_state, init_memory, None, None, None],
            name=_p(prefix, '_layers'), non_sequences=non_seqs, strict=True, n_steps=nsteps)
    return [rval, updates]


# initialize all parameters
def init_params(options):
    params = OrderedDict()
    params = get_layer('latent_lstm')[0](options, params,
                                         prefix='encoder',
                                         nin=options['dim_proj'],
                                         dim=options['dim'])
    params = get_layer('ff')[0](options, params, prefix='ff_in_lstm',
                                nin=options['dim_input'], nout=options['dim_proj'],
                                ortho=False)
    params = get_layer('ff')[0](options, params, prefix='ff_out_lstm',
                                nin=options['dim'], nout=options['dim_proj'],
                                ortho=False)
    params = get_layer('ff')[0](options, params, prefix='ff_out_prev',
                                nin=options['dim_proj'],
                                nout=options['dim_proj'], ortho=False)
    params = get_layer('hsoftmax')[0](options, params, prefix='ff_out_mus',
                                      nin=options['dim_proj'],
                                      ncls=300,
                                      nout=options['dim_input'])
    U = numpy.concatenate([norm_weight(options['dim_z'], options['dim']),
                           norm_weight(options['dim_z'], options['dim']),
                           norm_weight(options['dim_z'], options['dim']),
                           norm_weight(options['dim_z'], options['dim'])], axis=1)
    params[_p('z_cond', 'W')] = U

    params = get_layer(options['encoder'])[0](options, params,
                                              prefix='encoder_r',
                                              nin=options['dim_proj'],
                                              dim=options['dim'])
    # readout
    params = get_layer('ff')[0](options, params, prefix='ff_out_lstm_r',
                                nin=options['dim'], nout=options['dim_proj'],
                                ortho=False)
    params = get_layer('ff')[0](options, params, prefix='ff_out_prev_r',
                                nin=options['dim_proj'],
                                nout=options['dim_proj'], ortho=False)
    params = get_layer('hsoftmax')[0](options, params, prefix='ff_out_mus_r',
                                      nin=options['dim_proj'],
                                      ncls=300,
                                      nout=options['dim_input'])
    #Prior Network params
    params = get_layer('ff')[0](options, params, prefix='trans_1', nin=options['dim'], nout=options['prior_hidden'], ortho=False)
    params = get_layer('ff')[0](options, params, prefix='z_mus', nin=options['prior_hidden'], nout=2 * options['dim_z'], ortho=False)
    #Inference network params
    params = get_layer('ff')[0](options, params, prefix='inf', nin = 2 * options['dim'], nout=options['encoder_hidden'], ortho=False)
    params = get_layer('ff')[0](options, params, prefix='inf_mus', nin = options['encoder_hidden'], nout=2 * options['dim_z'], ortho=False)
    #Generative Network params
    params = get_layer('ff')[0](options, params, prefix='gen_mus', nin = options['dim_z'], nout=2 * options['dim'], ortho=False)
    return params


def build_rev_model(tparams, options, x, y, x_mask):
    # for the backward rnn, we just need to invert x and x_mask
    # concatenate first x and all targets y
    # x = [x1, x2, x3]
    # y = [x2, x3, x4]
    xc = tensor.concatenate([x[:1, :], y], axis=0)
    # xc = [x1, x2, x3, x4]
    xc_mask = tensor.concatenate([tensor.alloc(1, 1, x_mask.shape[1]), x_mask], axis=0)
    # xc_mask = [1, 1, 1, 0]
    # xr = [x4, x3, x2, x1]
    xr = xc[::-1]
    # xr_mask = [0, 1, 1, 1]
    xr_mask = xc_mask[::-1]

    xr_emb = get_layer('ff')[1](tparams, xr, options, prefix='ff_in_lstm', activ='lambda x: x')
    (states_rev, _), updates_rev = get_layer(options['encoder'])[1](tparams, xr_emb, options, prefix='encoder_r', mask=xr_mask)
    out_lstm = get_layer('ff')[1](tparams, states_rev, options, prefix='ff_out_lstm_r', activ='linear')
    out_prev = get_layer('ff')[1](tparams, xr_emb, options, prefix='ff_out_prev_r', activ='linear')
    out = lrelu(out_lstm + out_prev)
    # shift mus for prediction [o4, o3, o2]
    # targets are [x3, x2, x1]
    out = out[:-1]
    targets = xr[1:]
    targets_mask = xr_mask[1:]
    nll_rev = -get_layer('hsoftmax')[1](
        tparams, out, options, y_indexes=targets, prefix='ff_out_mus_r')
    # states_rev = [s4, s3, s2, s1]
    # cut first state out (info about x4 is in s3)
    # posterior sees (s1, s2, s3) in order to predict x2, x3, x4
    states_rev = states_rev[1:][::-1]
    # ...
    assert xr.ndim == 2
    assert xr_mask.ndim == 2
    nll_rev = (-tensor.log(nll_rev) * targets_mask).sum(0)
    return nll_rev, states_rev, updates_rev


# build a training model
def build_gen_model(tparams, options, x, y, x_mask, zmuv, states_rev):
    opt_ret = dict()
    # disconnecting reconstruction gradient from going in the backward encoder
    x_emb = get_layer('ff')[1](tparams, x, options, prefix='ff_in_lstm', activ='lambda x: x')
    rvals, updates_gen = get_layer('latent_lstm')[1](
        tparams, state_below=x_emb, options=options,
        prefix='encoder', mask=x_mask, gaussian_s=zmuv,
        back_states=states_rev)

    states_gen, z, kld, rec_cost_rev = rvals[0], rvals[2], rvals[3], rvals[4]
    # Compute parameters of the output distribution
    out_lstm = get_layer('ff')[1](tparams, states_gen, options, prefix='ff_out_lstm', activ='linear')
    out_prev = get_layer('ff')[1](tparams, x_emb, options, prefix='ff_out_prev', activ='linear')
    out = lrelu(out_lstm + out_prev)
    nll_gen = get_layer('hsoftmax')[1](
        tparams, out, options, y_indexes=y, prefix='ff_out_mus')
    nll_gen = (nll_gen * x_mask).sum(0)
    kld = (kld * x_mask).sum(0)
    rec_cost_rev = (rec_cost_rev * x_mask).sum(0)
    return nll_gen, states_gen, kld, rec_cost_rev, updates_gen


def ELBOcost(rec_cost, kld, kld_weight=1.):
    assert kld.ndim == 1
    assert rec_cost.ndim == 1
    return rec_cost + kld_weight * kld


def pred_probs(f_log_probs, options, data, source='valid'):
    rvals = []
    n_done = 0

    next_batch = (lambda: data.get_valid_batch()) \
        if source == 'valid' else (lambda: data.get_test_batch())
    for x, y, x_mask in next_batch():
        x = x.transpose(1, 0)
        y = y.transpose(1, 0)
        x_mask = x_mask.transpose(1, 0)
        n_done += x.shape[1]

        zmuv = numpy.random.normal(loc=0.0, scale=1.0, size=(
            x.shape[0], x.shape[1], options['dim_z'])).astype('float32')
        elbo = f_log_probs(x, y, x_mask, zmuv)
        for val in elbo:
            rvals.append(val)
    return numpy.array(rvals).mean()


# optimizers
# name(hyperp, tparams, grads, inputs (list), cost) = f_grad_shared, f_update
def adam(lr, tparams, gshared, beta1=0.9, beta2=0.99, e=1e-5):
    updates = []
    t_prev = theano.shared(numpy.float32(0.))
    t = t_prev + 1.
    lr_t = lr * tensor.sqrt(1. - beta2**t) / (1. - beta1**t)
    for p, g in zip(tparams.values(), gshared):
        m = theano.shared(p.get_value() * 0., p.name + '_mean')
        v = theano.shared(p.get_value() * 0., p.name + '_variance')
        m_t = beta1 * m + (1. - beta1) * g
        v_t = beta2 * v + (1. - beta2) * g**2
        step = lr_t * m_t / (tensor.sqrt(v_t) + e)
        p_t = p - step
        updates.append((m, m_t))
        updates.append((v, v_t))
        updates.append((p, p_t))
    updates.append((t_prev, t))
    f_update = theano.function([lr], [], updates=updates, profile=profile)
    return f_update


def train(dim_input=200,  # input vector dimensionality
          dim=2000,  # the number of GRU units
          dim_proj=600,  # the number of GRU units
          encoder='lstm',
          patience=10,  # early stopping patience
          max_epochs=5000,
          finish_after=10000000,  # finish after this many updates
          dispFreq=100,
          decay_c=0.,  # L2 weight decay penalty
          lrate=0.001,
          maxlen=100,  # maximum length of the description
          optimizer='adam',
          batch_size=16,
          valid_batch_size=16,
          saveto='model.npz',
          validFreq=1000,
          saveFreq=1000,  # save the parameters after every saveFreq updates
          sampleFreq=100,  # generate some samples after every sampleFreq
          dataset=None,  # Not used
          valid_dataset=None,  # Not used
          dictionary=None,  # Not used
          use_dropout=False,
          reload_=False,
          kl_start=0.2,
          weight_aux=0.,
          kl_rate=0.0003):

    prior_hidden = dim
    dim_z = 100
    encoder_hidden = dim
    learn_h0 = False

    desc = saveto + 'seed_' + str(seed) + '_model_' + str(weight_aux) + '_weight_aux_' + \
        str(kl_start) + '_kl_Start_' + str(kl_rate) +  '_kl_rate_log.txt'
    opts = saveto + 'seed_' + str(seed) + '_model_' + str(weight_aux) + '_weight_aux_' + \
        str(kl_start) + '_kl_Start_' + str(kl_rate) +  '_kl_rate_opts.pkl'

    print(desc)

    # Model options
    model_options = locals().copy()
    pkl.dump(model_options, open(opts, 'wb'))
    log_file = open(desc, 'w')

    data = Text8("./t8/data", 35, batch_size=model_options['batch_size'])

    print('Building model')
    params = init_params(model_options)
    tparams = init_tparams(params)

    x = tensor.lmatrix('x')
    y = tensor.lmatrix('y')
    x_mask = tensor.matrix('x_mask')
    zmuv = tensor.tensor3('zmuv')
    weight_f = tensor.scalar('weight_f')
    lr = tensor.scalar('lr')

    # build the symbolic computational graph
    nll_rev, states_rev, updates_rev = \
        build_rev_model(tparams, model_options, x, y, x_mask)
    nll_gen, states_gen, kld, rec_cost_rev, updates_gen = \
        build_gen_model(tparams, model_options, x, y, x_mask, zmuv, states_rev)

    vae_cost = ELBOcost(nll_gen, kld, kld_weight=weight_f).mean()
    elbo_cost = ELBOcost(nll_gen, kld, kld_weight=1.).mean()
    aux_cost = (numpy.float32(weight_aux) * (rec_cost_rev + nll_rev)).mean()
    tot_cost = (vae_cost + aux_cost)
    nll_gen_cost = nll_gen.mean()
    nll_rev_cost = nll_rev.mean()
    kld_cost = kld.mean()

    print('Building f_log_probs...')
    inps = [x, y, x_mask, zmuv, weight_f]
    f_log_probs = theano.function(
        inps[:-1], ELBOcost(nll_gen, kld, kld_weight=1.),
        updates=(updates_gen + updates_rev), profile=profile)
    print('Done')

    print('Computing gradient...')
    grads = tensor.grad(tot_cost, itemlist(tparams))
    print('Done')

    all_grads, non_finite, clipped = gradient_clipping(grads, tparams, 5.)
    # update function
    all_gshared = [theano.shared(p.get_value() * 0., name='%s_grad' % k)
                   for k, p in tparams.iteritems()]
    all_gsup = [(gs, g) for gs, g in zip(all_gshared, all_grads)]
    # forward pass + gradients
    outputs = [vae_cost, aux_cost, tot_cost, kld_cost, elbo_cost, nll_rev_cost, nll_gen_cost, non_finite]
    print('Fprop')
    f_prop = theano.function(inps, outputs, updates=all_gsup)
    print('Fupdate')
    f_update = eval(optimizer)(lr, tparams, all_gshared)

    print('Optimization')
    history_errs = []
    # reload history
    if reload_ and os.path.exists(saveto):
        history_errs = list(numpy.load(saveto)['history_errs'])
    best_p = None
    bad_count = 0

    # Training loop
    uidx = 0
    estop = False
    bad_counter = 0
    kl_start = model_options['kl_start']
    kl_rate = model_options['kl_rate']

    for eidx in range(max_epochs):
        print("Epoch: {}".format(eidx))
        n_samples = 0
        tr_costs = [[], [], [], [], [], [], []]

        for x, y, x_mask in data.get_train_batch():
            # Transpose data to have the time steps on dimension 0.
            x = x.transpose(1, 0).astype('int32')
            y = y.transpose(1, 0).astype('int32')
            x_mask = x_mask.transpose(1, 0).astype('float32')

            n_samples += x.shape[1]
            uidx += 1
            if kl_start < 1.:
                kl_start += kl_rate

            ud_start = time.time()
            # compute cost, grads and copy grads to shared variables
            zmuv = numpy.random.normal(loc=0.0, scale=1.0, size=(x.shape[0], x.shape[1], model_options['dim_z'])).astype('float32')
            vae_cost_np, aux_cost_np, tot_cost_np, kld_cost_np, \
                elbo_cost_np, nll_rev_cost_np, nll_gen_cost_np, not_finite_np = \
                f_prop(x, y, x_mask, zmuv, np.float32(kl_start))
            if numpy.isnan(tot_cost_np) or numpy.isinf(tot_cost_np) or not_finite_np:
                print('Nan cost... skipping')
                continue
            else:
                f_update(numpy.float32(lrate))

            # update costs
            tr_costs[0].append(vae_cost_np)
            tr_costs[1].append(aux_cost_np)
            tr_costs[2].append(tot_cost_np)
            tr_costs[3].append(kld_cost_np)
            tr_costs[4].append(elbo_cost_np)
            tr_costs[5].append(nll_rev_cost_np)
            tr_costs[6].append(nll_gen_cost_np)
            ud = time.time() - ud_start

            # verbose
            if numpy.mod(uidx, dispFreq) == 0:
                str1 = 'Epoch {:d}  Update {:d}  VaeCost {:.2f}  AuxCost {:.2f}  KldCost {:.2f}  TotCost {:.2f}  ElboCost {:.2f}  NllRev {:.2f}  NllGen {:.2f}  KL_start {:.2f}'.format(
                    eidx, uidx, np.mean(tr_costs[0]), np.mean(tr_costs[1]), np.mean(tr_costs[3]), np.mean(tr_costs[2]), np.mean(tr_costs[4]), \
                    np.mean(tr_costs[5]), np.mean(tr_costs[6]), kl_start)
                print(str1)
                log_file.write(str1 + '\n')
                log_file.flush()

        if eidx in [10, 20]:
            lrate = lrate / 2.0

        print 'Starting validation...'
        valid_err = pred_probs(f_log_probs, model_options, data, source='valid')
        test_err = pred_probs(f_log_probs, model_options, data, source='test')
        history_errs.append(valid_err)
        str1 = 'Valid/Test ELBO: {:.2f}, {:.2f}'.format(valid_err, test_err)
        print(str1)
        log_file.write(str1 + '\n')

        # finish after this many updates
        if uidx >= finish_after:
            print('Finishing after %d iterations!' % uidx)
            break

    valid_err = pred_probs(f_log_probs, model_options, data, source='valid')
    test_err = pred_probs(f_log_probs, model_options, data, source='test')
    str1 = 'Valid/Test ELBO: {:.2f}, {:.2f}'.format(valid_err, test_err)
    print(str1)
    log_file.write(str1 + '\n')
    log_file.close()
    return valid_err


if __name__ == '__main__':
    pass
