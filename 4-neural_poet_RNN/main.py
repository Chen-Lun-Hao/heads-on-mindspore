'''main'''
# coding:utf8
# pylint: disable = R1723, E1130, W0612
import sys
import os
import tqdm
import ipdb

from data import get_data
from model import PoetryModel

from mindspore import load_checkpoint, load_param_into_net, context, Tensor, value_and_grad, save_checkpoint
import mindspore.dataset as ds
import mindspore.nn as nn
from mindspore.train import Loss
from mindspore.train.summary import SummaryRecord

class Config():
    '''配置类'''
    data_path = 'data/'  # 诗歌的文本文件存放路径
    pickle_path = 'tang.npz'  # 预处理好的二进制文件
    author = None  # 只学习某位作者的诗歌
    constrain = None  # 长度限制
    category = 'poet.tang'  # 类别，唐诗还是宋诗歌(poet.song)
    lr = 1e-3
    weight_decay = 1e-4
    use_gpu = True
    epoch = 20
    batch_size = 128
    maxlen = 125  # 超过这个长度的之后字被丢弃，小于这个长度的在前面补空格
    plot_every = 20  # 每20个batch 可视化一次
    # use_env = True # 是否使用visodm
    env = 'poetry'  # visdom env
    max_gen_len = 200  # 生成诗歌最长长度
    debug_file = '/tmp/debugp'
    model_path = None  # 预训练模型路径
    prefix_words = '细雨鱼儿出,微风燕子斜。'  # 不是诗歌的组成部分，用来控制生成诗歌的意境
    start_words = '闲云潭影日悠悠'  # 诗歌开始
    acrostic = False  # 是否是藏头诗
    model_prefix = 'checkpoints/tang'  # 模型保存路径


opt = Config()
if opt.use_gpu:
    context.set_context(mode=context.GRAPH_MODE, device_target="GPU")
else:
    context.set_context(mode=context.GRAPH_MODE, device_target="CPU")

def generate(model, start_words, ix2word, word2ix, prefix_words=None):
    """
    给定几个词，根据这几个词接着生成一首完整的诗歌
    start_words：u'春江潮水连海平'
    比如start_words 为 春江潮水连海平，可以生成：

    """

    results = list(start_words)
    start_word_len = len(start_words)
    # 手动设置第一个词为<START>
    x = Tensor([word2ix['<START>']]).view(1, 1).long()
    hidden = None

    if prefix_words:
        for word in prefix_words:
            output, hidden = model(x, hidden)
            x = x.data.new([word2ix[word]]).view(1, 1)

    for i in range(opt.max_gen_len):
        output, hidden = model(x, hidden)

        if i < start_word_len:
            w = results[i]
            x = x.data.new([word2ix[w]]).view(1, 1)
        else:
            top_index = output.data[0].topk(1)[1][0].item()
            w = ix2word[top_index]
            results.append(w)
            x = x.data.new([top_index]).view(1, 1)
        if w == '<EOP>':
            del results[-1]
            break
    return results


def gen_acrostic(model, start_words, ix2word, word2ix, prefix_words=None):
    """
    生成藏头诗
    start_words : u'深度学习'
    生成：
    深木通中岳，青苔半日脂。
    度山分地险，逆浪到南巴。
    学道兵犹毒，当时燕不移。
    习根通古岸，开镜出清羸。
    """
    results = []
    start_word_len = len(start_words)
    x = (Tensor([word2ix['<START>']]).view(1, 1).long())
    hidden = None

    index = 0  # 用来指示已经生成了多少句藏头诗
    # 上一个词
    pre_word = '<START>'

    if prefix_words:
        for word in prefix_words:
            output, hidden = model(x, hidden)
            x = (x.data.new([word2ix[word]])).view(1, 1)

    for _ in range(opt.max_gen_len):
        output, hidden = model(x, hidden)
        top_index = output.data[0].topk(1)[1][0].item()
        w = ix2word[top_index]

        if (pre_word in {u'。', u'！', '<START>'}):
            # 如果遇到句号，藏头的词送进去生成

            if index == start_word_len:
                # 如果生成的诗歌已经包含全部藏头的词，则结束
                break
            else:
                # 把藏头的词作为输入送入模型
                w = start_words[index]
                index += 1
                x = (x.data.new([word2ix[w]])).view(1, 1)
        else:
            # 否则的话，把上一次预测是词作为下一个词输入
            x = (x.data.new([word2ix[w]])).view(1, 1)
        results.append(w)
        pre_word = w
    return results

def train(**kwargs):
    '''模型训练，传入参数参考配置类'''
    for k, v in kwargs.items():
        setattr(opt, k, v)

    # 获取数据
    data, word2ix, ix2word = get_data(opt)
    data = Tensor.from_numpy(data)
    dataloader = ds.GeneratorDataset(data, shuffle=True, num_parallel_workers=1)
    dataloader = dataloader.batch(batch_size=opt.batch_size)

    # 模型定义
    model = PoetryModel(len(word2ix), 128, 256)
    optimizer = nn.Adam(model.trainable_params(), opt.lr)
    criterion = nn.CrossEntropyLoss()
    if opt.model_path:
        # 将模型参数存入parameter的字典中，这里加载的是上面训练过程中保存的模型参数
        param_dict = load_checkpoint(opt.model_path)
        # 将参数加载到网络中
        load_param_into_net(model, param_dict)

    loss_meter = Loss()

   # 前向传播
    def forward_fn(data, label):
        logits = model(data)
        loss = criterion(logits, label.view(-1))
        return loss, logits

    # 梯度函数
    grad_fn = value_and_grad(
        forward_fn, None, optimizer.parameters, has_aux=True)

    # 更新，训练
    def train_step(data, label):
        (loss, logits), grads = grad_fn(data, label)
        optimizer(grads)
        return loss, logits
    with SummaryRecord(log_dir="./summary_dir", network=model) as summary_record:
        for epoch in range(opt.epoch):
            loss_meter.clear()
            for ii, data_ in tqdm.tqdm(enumerate(dataloader)):

                # 训练
                data_ = data_.long().transpose(1, 0).contiguous()
                input_, target = data_[:-1, :], data_[1:, :]
                # 损失值以及预测值
                loss, _ = train_step(input_, target)

                loss_meter.update(loss.item())

                # 可视化
                if (1 + ii) % opt.plot_every == 0:

                    if os.path.exists(opt.debug_file):
                        ipdb.set_trace()

                    summary_record.add_value(
                        'scalar', 'loss', loss_meter.eval())

                    # 诗歌原文
                    poetrys = [[ix2word[_word] for _word in data_[:, _iii].tolist()]
                        for _iii in range(data_.shape[1])][:16]
                    with open('origin_poem','a') as f:
                        test = '</br>'.join([''.join(poetry) for poetry in poetrys])
                        f.write(test)

                    gen_poetries = []
                    # 分别以这几个字作为诗歌的第一个字，生成8首诗
                    for word in list(u'春江花月夜凉如水'):
                        gen_poetry = ''.join(generate(model, word, ix2word, word2ix))
                        gen_poetries.append(gen_poetry)
                    with open('gen_poem','a', encoding='utf-8') as f:
                        test = '</br>'.join([''.join(poetry) for poetry in gen_poetries])
                        f.write(test)
        save_checkpoint(model, '%s_%s.ckpt' % (opt.model_prefix, epoch))


def gen(**kwargs):
    """
    提供命令行接口，用以生成相应的诗
    """

    for k, v in kwargs.items():
        setattr(opt, k, v)
    data, word2ix, ix2word = get_data(opt)
    model = PoetryModel(len(word2ix), 128, 256)
    # 将模型参数存入parameter的字典中，这里加载的是上面训练过程中保存的模型参数
    param_dict = load_checkpoint(opt.model_path)
    # 将参数加载到网络中
    load_param_into_net(model, param_dict)

    if opt.use_gpu:
        model.cuda()

    # python2和python3 字符串兼容
    if sys.version_info.major == 3:
        if opt.start_words.isprintable():
            start_words = opt.start_words
            prefix_words = opt.prefix_words if opt.prefix_words else None
        else:
            start_words = opt.start_words.encode('ascii', 'surrogateescape').decode('utf8')
            prefix_words = opt.prefix_words.encode('ascii', 'surrogateescape').decode(
                'utf8') if opt.prefix_words else None
    else:
        start_words = opt.start_words.decode('utf8')
        prefix_words = opt.prefix_words.decode('utf8') if opt.prefix_words else None

    start_words = start_words.replace(',', '，') \
        .replace('.', '。') \
        .replace('?', '？')

    gen_poetry = gen_acrostic if opt.acrostic else generate
    result = gen_poetry(model, start_words, ix2word, word2ix, prefix_words)
    print(''.join(result))


if __name__ == '__main__':
    import fire

    fire.Fire()
