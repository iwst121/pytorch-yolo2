from __future__ import print_function
import sys
if len(sys.argv) != 4:
    print('Usage:')
    print('python train.py datacfg cfgfile weightfile')
    exit()

import time
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.backends.cudnn as cudnn
from torchvision import datasets, transforms
from torch.autograd import Variable

import dataset
import random
import math
from utils import *
from cfg import parse_cfg
from darknet import Darknet
from models.tiny_yolo import TinyYoloNet


# Training settings
datacfg       = sys.argv[1]
cfgfile       = sys.argv[2]
weightfile    = sys.argv[3]

data_options  = read_data_cfg(datacfg)
net_options   = parse_cfg(cfgfile)[0]

trainlist     = data_options['train']
testlist      = data_options['valid']
backupdir     = data_options['backup']
nsamples      = file_lines(trainlist)
gpus          = data_options['gpus']  # e.g. 0,1,2,3

batch_size    = int(net_options['batch'])
max_batches   = int(net_options['max_batches'])
learning_rate = float(net_options['learning_rate'])
momentum      = float(net_options['momentum'])


#Train parameters
max_epochs    = max_batches*batch_size/nsamples+1
use_cuda      = True
seed          = 22222
eps           = 1e-5

epoch_step    = 50 # epochs to change lr
lr_step       = 0.1
num_workers   = 10
save_interval = 10  # epoches
dot_interval  = 70  # batches

# Test parameters
conf_thresh   = 0.25
nms_thresh    = 0.4
iou_thresh    = 0.5

###############
torch.manual_seed(seed)
if use_cuda:
    os.environ['CUDA_VISIBLE_DEVICES'] = gpus
    torch.cuda.manual_seed(seed)

model       = Darknet(cfgfile)
#model       = TinyYoloNet()

model.load_weights(weightfile)
model.print_network()
init_epoch = model.seen / nsamples 

kwargs = {'num_workers': num_workers, 'pin_memory': True} if use_cuda else {}
test_loader = torch.utils.data.DataLoader(
    dataset.listDataset(testlist, shape=(model.width, model.height),
                   shuffle=False,
                   transform=transforms.Compose([
                       transforms.ToTensor(),
                   ]), train=False),
    batch_size=batch_size, shuffle=False, **kwargs)

if use_cuda:
    model = torch.nn.DataParallel(model).cuda()

optimizer = optim.SGD(model.parameters(), lr=learning_rate, momentum=momentum)

def adjust_learning_rate(optimizer, epoch):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    lr = learning_rate * (lr_step ** (epoch // epoch_step))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    if epoch % epoch_step == 0:
        logging('lr = %f' % (lr))

def train(epoch):
    t0 = time.time()
    train_loader = torch.utils.data.DataLoader(
        dataset.listDataset(trainlist, shape=(model.module.width, model.module.height),
                       shuffle=True,
                       transform=transforms.Compose([
                           transforms.ToTensor(),
                       ]), train=True, seen=model.module.seen),
        batch_size=batch_size, shuffle=False, **kwargs)

    logging('epoch %d : processed %d samples' % (epoch, epoch * len(train_loader.dataset)))
    model.train()
    adjust_learning_rate(optimizer, epoch)
    t1 = time.time()
    avg_time = torch.zeros(9)
    for batch_idx, (data, target) in enumerate(train_loader):
        t2 = time.time()
        if (batch_idx+1) % dot_interval == 0:
            sys.stdout.write('.')

        if use_cuda:
            data = data.cuda()
            #target= target.cuda()
        t3 = time.time()
        data, target = Variable(data), Variable(target)
        t4 = time.time()
        optimizer.zero_grad()
        t5 = time.time()
        loss = model(data, target).sum()
        t6 = time.time()
        t7 = time.time()
        loss.backward()
        t8 = time.time()
        optimizer.step()
        t9 = time.time()
        if False and batch_idx > 1:
            avg_time[0] = avg_time[0] + (t2-t1)
            avg_time[1] = avg_time[1] + (t3-t2)
            avg_time[2] = avg_time[2] + (t4-t3)
            avg_time[3] = avg_time[3] + (t5-t4)
            avg_time[4] = avg_time[4] + (t6-t5)
            avg_time[5] = avg_time[5] + (t7-t6)
            avg_time[6] = avg_time[6] + (t8-t7)
            avg_time[7] = avg_time[7] + (t9-t8)
            avg_time[8] = avg_time[8] + (t9-t1)
            print('-------------------------------')
            print('       load data : %f' % (avg_time[0]/(batch_idx)))
            print('     cpu to cuda : %f' % (avg_time[1]/(batch_idx)))
            print('cuda to variable : %f' % (avg_time[2]/(batch_idx)))
            print('       zero_grad : %f' % (avg_time[3]/(batch_idx)))
            print(' forward feature : %f' % (avg_time[4]/(batch_idx)))
            print('    forward loss : %f' % (avg_time[5]/(batch_idx)))
            print('        backward : %f' % (avg_time[6]/(batch_idx)))
            print('            step : %f' % (avg_time[7]/(batch_idx)))
            print('           total : %f' % (avg_time[8]/(batch_idx)))
        t1 = time.time()
    print('')
    t1 = time.time()
    logging('training with %f samples/s' % (len(train_loader.dataset)/(t1-t0)))
    if (epoch+1) % save_interval == 0:
        logging('save weights to %s/%06d.weights' % (backupdir, epoch+1))
        model.module.seen = (epoch + 1) * len(train_loader.dataset)
        model.module.save_weights('%s/%06d.weights' % (backupdir, epoch+1))

def test(epoch):
    def truths_length(truths):
        for i in range(50):
            if truths[i][1] == 0:
                return i

    model.eval()
    num_classes = model.module.num_classes
    anchors     = model.module.anchors
    num_anchors = model.module.num_anchors
    total       = 0.0
    proposals   = 0.0
    correct     = 0.0

    for batch_idx, (data, target) in enumerate(test_loader):
        if use_cuda:
            data = data.cuda()
        data = Variable(data, volatile=True)
        output = model(data).data
        all_boxes = get_region_boxes(output, conf_thresh, num_classes, anchors, num_anchors)
        for i in range(output.size(0)):
            boxes = all_boxes[i]
            boxes = nms(boxes, nms_thresh)
            truths = target[i].view(-1, 5)
            num_gts = truths_length(truths)
     
            total = total + num_gts
    
            for i in range(len(boxes)):
                if boxes[i][4] > conf_thresh:
                    proposals = proposals+1

            for i in range(num_gts):
                box_gt = [truths[i][1], truths[i][2], truths[i][3], truths[i][4], 1.0, 1.0, truths[i][0]]
                best_iou = 0
                for j in range(len(boxes)):
                    iou = bbox_iou(box_gt, boxes[j], x1y1x2y2=False)
                    best_iou = max(iou, best_iou)
                if best_iou > iou_thresh and boxes[j][6] == box_gt[6]:
                    correct = correct+1

    precision = 1.0*correct/(proposals+eps)
    recall = 1.0*correct/(total+eps)
    fscore = 2.0*precision*recall/(precision+recall+eps)
    logging("precision: %f, recall: %f, fscore: %f" % (precision, recall, fscore))

evaluate = False
if evaluate:
    print('evaluating ...')
    test(0)
else:
    for epoch in range(init_epoch, max_epochs): 
        train(epoch)
        test(epoch)
