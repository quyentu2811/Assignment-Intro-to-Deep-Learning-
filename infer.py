#!/usr/bin/env python
# coding: utf-8

# In[29]:


get_ipython().system('pip install torchsummary')
get_ipython().system('pip install torchgeometry')
get_ipython().system('pip install wandb')


# In[30]:


from torchsummary import summary
from torchgeometry.losses import one_hot
import os
import pandas as pd
import numpy as np
from PIL import Image
import cv2
import time
import imageio
import matplotlib.pyplot as plt
import time
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.optim import lr_scheduler
from torch import Tensor
from torch.utils.data import Dataset, DataLoader, random_split
from torchvision.transforms import Resize, PILToTensor, ToPILImage, Compose, InterpolationMode
from torchvision.transforms import RandomHorizontalFlip, RandomVerticalFlip, RandomRotation
from collections import OrderedDict
import wandb


# In[31]:


get_ipython().system('nvidia-smi -L')


# In[32]:


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
device


# # PARAMETERS

# In[33]:


# Number of class in the data set (3: neoplastic, non neoplastic, background)
num_classes = 3

# Number of epoch
epochs = 100

# Hyperparameters for training
learning_rate = 2e-04
batch_size = 4
display_step = 50


# Initialize lists to keep track of loss and accuracy
loss_epoch_array = []
train_accuracy = []
test_accuracy = []
valid_accuracy = []


# # DATALOADER

# In[34]:


train_transform = Compose([
    Resize((256, 256), interpolation=InterpolationMode.BILINEAR),  # Adjust the size according to your needs
    RandomHorizontalFlip(),
    RandomVerticalFlip(),
    RandomRotation(degrees = 30),
    PILToTensor(),  # Convert the PIL image to a PyTorch tensor
    lambda x: x.float(),
])
valid_transform = Compose([Resize((256, 256), interpolation=InterpolationMode.BILINEAR),
                     PILToTensor()])


# In[35]:


class UNetDataClass(Dataset):
    def __init__(self, images_path, masks_path, train_transform, valid_transform):
        super(UNetDataClass, self).__init__()

        images_list = os.listdir(images_path)
        masks_list = os.listdir(masks_path)

        images_list = [images_path + image_name for image_name in images_list]
        masks_list = [masks_path + mask_name for mask_name in masks_list]

        self.images_list = images_list
        self.masks_list = masks_list
        self.train_transform = train_transform
        self.valid_transform = valid_transform

    def __getitem__(self, index):
        img_path = self.images_list[index]
        mask_path = self.masks_list[index]

        # Open image and mask
        data = Image.open(img_path)
        label = Image.open(mask_path)

        # Normalize
        data = self.train_transform(data) / 255
        label = self.valid_transform(label) / 255

        label = torch.where(label>0.65, 1.0, 0.0)

        label[2, :, :] = 0.0001
        label = torch.argmax(label, 0).type(torch.int64)

        return data, label

    def __len__(self):
        return len(self.images_list)


# In[36]:


images_path = '/kaggle/input/bkai-igh-neopolyp/train/train/'
masks_path = '/kaggle/input/bkai-igh-neopolyp/train_gt/train_gt/'


# In[37]:


unet_dataset = UNetDataClass(images_path, masks_path, train_transform, valid_transform)


# In[38]:


train_size = 0.8
valid_size = 0.2


# In[39]:


train_set, valid_set = random_split(unet_dataset,
                                    [int(train_size * len(unet_dataset)) ,
                                     int(valid_size * len(unet_dataset))])


# In[40]:


train_dataloader = DataLoader(train_set, batch_size=batch_size, shuffle=True)
valid_dataloader = DataLoader(valid_set, batch_size=batch_size, shuffle=True)


# # MODEL
# 

# ## Encoder Block

# In[41]:


class encoder_block(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(encoder_block, self).__init__()

        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=1, padding='same')
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding='same')

        self.bn1 = nn.BatchNorm2d(out_channels)
        self.bn2 = nn.BatchNorm2d(out_channels)

        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(p=0.3)
        self.max_pool = nn.MaxPool2d(kernel_size=2, stride=2)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)

        x = self.dropout(x)

        x = self.conv2(x)
        x = self.bn2(x)
        x = self.relu(x)

        next_layer = self.max_pool(x)
        skip_layer = x

        return next_layer, skip_layer


# ## Decoder block

# In[42]:


class decoder_block(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(decoder_block, self).__init__()

        self.transpose_conv = nn.ConvTranspose2d(in_channels, out_channels, kernel_size=2, stride=2)

        self.conv1 = nn.Conv2d(2 * out_channels, out_channels, kernel_size=3, stride=1, padding='same')
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding='same')

        self.bn1 = nn.BatchNorm2d(out_channels)
        self.bn2 = nn.BatchNorm2d(out_channels)

        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(p=0.3)

    def forward(self, x, skip_layer):
        x = self.transpose_conv(x)
        x = torch.cat([x, skip_layer], axis=1)

        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)

        x = self.dropout(x)

        x = self.conv2(x)
        x = self.bn2(x)
        x = self.relu(x)

        return x


# ## Bottle neck

# In[43]:


class bottleneck_block(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(bottleneck_block, self).__init__()

        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=1, padding='same')
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding='same')

        self.bn1 = nn.BatchNorm2d(out_channels)
        self.bn2 = nn.BatchNorm2d(out_channels)

        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(p=0.3)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)

        x = self.dropout(x)

        x = self.conv2(x)
        x = self.bn2(x)
        x = self.relu(x)

        return x


# ## Unet model

# In[44]:


# UNet model
class UNet(nn.Module):
    def __init__(self, n_class=3):
        super(UNet, self).__init__()
        # Encoder blocks
        self.enc1 = encoder_block(3, 64)
        self.enc2 = encoder_block(64, 128)
        self.enc3 = encoder_block(128, 256)
        self.enc4 = encoder_block(256, 512)

        # Bottleneck block
        self.bottleneck = bottleneck_block(512, 1024)

        # Decoder blocks
        self.dec1 = decoder_block(1024, 512)
        self.dec2 = decoder_block(512, 256)
        self.dec3 = decoder_block(256, 128)
        self.dec4 = decoder_block(128, 64)

        # 1x1 convolution
        self.out = nn.Conv2d(64, n_class, kernel_size=1, padding='same')

    def forward(self, image):
        n1, s1 = self.enc1(image)
        n2, s2 = self.enc2(n1)
        n3, s3 = self.enc3(n2)
        n4, s4 = self.enc4(n3)

        n5 = self.bottleneck(n4)

        n6 = self.dec1(n5, s4)
        n7 = self.dec2(n6, s3)
        n8 = self.dec3(n7, s2)
        n9 = self.dec4(n8, s1)

        output = self.out(n9)

        return output


# # LOSS FUNCTION
# 

# In[45]:


class CEDiceLoss(nn.Module):
    def __init__(self, weights) -> None:
        super(CEDiceLoss, self).__init__()
        self.eps: float = 1e-6
        self.weights: torch.Tensor = weights

    def forward(
            self,
            input: torch.Tensor,
            target: torch.Tensor) -> torch.Tensor:
        if not torch.is_tensor(input):
            raise TypeError("Input type is not a torch.Tensor. Got {}"
                            .format(type(input)))
        if not len(input.shape) == 4:
            raise ValueError("Invalid input shape, we expect BxNxHxW. Got: {}"
                             .format(input.shape))
        if not input.shape[-2:] == target.shape[-2:]:
            raise ValueError("input and target shapes must be the same. Got: {}"
                             .format(input.shape, input.shape))
        if not input.device == target.device:
            raise ValueError(
                "input and target must be in the same device. Got: {}" .format(
                    input.device, target.device))
        if not self.weights.shape[1] == input.shape[1]:
            raise ValueError("The number of weights must equal the number of classes")
        if not torch.sum(self.weights).item() == 1:
            raise ValueError("The sum of all weights must equal 1")

        # cross entropy loss
        celoss = nn.CrossEntropyLoss(self.weights)(input, target)

        # compute softmax over the classes axis
        input_soft = F.softmax(input, dim=1)

        # create the labels one hot tensor
        target_one_hot = one_hot(target, num_classes=input.shape[1],
                                 device=input.device, dtype=input.dtype)

        # compute the actual dice score
        dims = (2, 3)
        intersection = torch.sum(input_soft * target_one_hot, dims)
        cardinality = torch.sum(input_soft + target_one_hot, dims)

        dice_score = 2. * intersection / (cardinality + self.eps)

        dice_score = torch.sum(dice_score * self.weights, dim=1)

        return torch.mean(1. - dice_score) + celoss


# # TRAINING

# ## Initialize weights

# In[46]:


def weights_init(model):
    if isinstance(model, nn.Linear):
        # Xavier Distribution
        torch.nn.init.xavier_uniform_(model.weight)


# In[47]:


def save_model(model, optimizer, path):
    checkpoint = {
        "model": model.state_dict(),
        "optimizer": optimizer.state_dict(),
    }
    torch.save(checkpoint, path)

def load_model(model, optimizer, path):
    checkpoint = torch.load(path)
    model.load_state_dict(checkpoint["model"])
    optimizer.load_state_dict(checkpoint['optimizer'])
    return model, optimizer


# ## Train model

# In[48]:


# Train function for each epoch
def train(train_dataloader, valid_dataloader,learing_rate_scheduler, epoch, display_step):
    print(f"Start epoch #{epoch+1}, learning rate for this epoch: {learing_rate_scheduler.get_last_lr()}")
    start_time = time.time()
    train_loss_epoch = 0
    test_loss_epoch = 0
    last_loss = 999999999
    model.train()
    for i, (data,targets) in enumerate(train_dataloader):

        # Load data into GPU
        data, targets = data.to(device), targets.to(device)

        optimizer.zero_grad()
        outputs = model(data)

        # Backpropagation, compute gradients
        loss = loss_function(outputs, targets.long())
        loss.backward()

        # Apply gradients
        optimizer.step()

        # Save loss
        train_loss_epoch += loss.item()
        if (i+1) % display_step == 0:
#             accuracy = float(test(test_loader))
            print('Train Epoch: {} [{}/{} ({}%)]\tLoss: {:.4f}'.format(
                epoch + 1, (i+1) * len(data), len(train_dataloader.dataset), 100 * (i+1) * len(data) / len(train_dataloader.dataset),
                loss.item()))

    print(f"Done epoch #{epoch+1}, time for this epoch: {time.time()-start_time}s")
    train_loss_epoch/= (i + 1)

    # Evaluate the validation set
    model.eval()
    with torch.no_grad():
        for data, target in valid_dataloader:
            data, target = data.to(device), target.to(device)
            test_output = model(data)
            test_loss = loss_function(test_output, target)
            test_loss_epoch += test_loss.item()

    test_loss_epoch/= (i+1)

    return train_loss_epoch , test_loss_epoch


# ## Test function

# In[49]:


# Test function
def test(dataloader):
    test_loss = 0
    correct = 0
    with torch.no_grad():
        for i, (data, targets) in enumerate(dataloader):
            data, targets = data.to(device), targets.to(device)
            outputs = model(data)
            _, pred = torch.max(outputs, 1)
            test_loss += targets.size(0)
            correct += torch.sum(pred == targets).item()
    return correct / test_loss


# In[50]:


pretrained_path = '/kaggle/working/unet_model.pth'


# In[51]:


model = UNet()
model.apply(weights_init)
checkpoint = torch.load(pretrained_path)

# new_state_dict = OrderedDict()
# for k, v in checkpoint['model'].items():
#     name = k[7:] # remove `module.`
#     new_state_dict[name] = v
# load params
# model.load_state_dict(new_state_dict)
model = nn.DataParallel(model)
model.to(device)


# In[52]:


import torch
print(torch.cuda.is_available())
import torchvision
weights = torch.Tensor([[0.4, 0.55, 0.05]]).cuda()
loss_function = CEDiceLoss(weights)

# Define the optimizer (Adam optimizer)
optimizer = optim.Adam(params=model.parameters(), lr=learning_rate)
# optimizer.load_state_dict(checkpoint['optimizer'])

# Learning rate scheduler
learing_rate_scheduler = lr_scheduler.StepLR(optimizer, step_size=4, gamma=0.6)

checkpoint_path = '/kaggle/working/unet_model.pth' #the path to save the trained model


# In[53]:


save_model(model, optimizer, checkpoint_path)


# In[54]:


import torch
torch.cuda.empty_cache()
wandb.login(
    key = "c456a83d581dce980c77ae1bf7613173858779c8",
)
wandb.init(project= "PolypSegment")
#wandb.init(timeout=300)
# Training loop
train_loss_array = []
test_loss_array = []
last_loss = 9999999999999
for epoch in range(epochs):
    train_loss_epoch = 0
    test_loss_epoch = 0
    (train_loss_epoch, test_loss_epoch) = train(train_dataloader,
                                              valid_dataloader,
                                              learing_rate_scheduler, epoch, display_step)

    if test_loss_epoch < last_loss:
        save_model(model, optimizer, checkpoint_path)
        last_loss = test_loss_epoch

    learing_rate_scheduler.step()
    train_loss_array.append(train_loss_epoch)
    test_loss_array.append(test_loss_epoch)
    wandb.log({"Train loss": train_loss_epoch, "Valid loss": test_loss_epoch})
    train_accuracy.append(test(train_dataloader))
    valid_accuracy.append(test(valid_dataloader))
    print("Epoch {}: loss: {:.4f}, train accuracy: {:.4f}, valid accuracy:{:.4f}".format(epoch + 1,
                                        train_loss_array[-1], train_accuracy[-1], valid_accuracy[-1]))


# # Plot the learning cure

# In[56]:


checkpoint_path = '/kaggle/working/unet_model.pth'
loaded_model, loaded_optimizer = load_model(model, optimizer, checkpoint_path)


# In[57]:


plt.rcParams['figure.dpi'] = 90
plt.rcParams['figure.figsize'] = (6, 4)
epochs_array = range(epochs)


# In[58]:


# Plot Training and Test loss
plt.plot(epochs_array, train_loss_array, 'g', label='Training loss')
# plt.plot(epochs_array, test_loss_array, 'b', label='Test loss')
plt.title('Training and Test loss')
plt.xlabel('Epochs')
plt.ylabel('Loss')
plt.legend()
plt.show()


# # Visualize results

# In[59]:


for i, (data, label) in enumerate(train_dataloader):
    img = data
    mask = label
    break


# In[60]:


fig, arr = plt.subplots(4, 3, figsize=(16, 12))
arr[0][0].set_title('Image')
arr[0][1].set_title('Segmentation')
arr[0][2].set_title('Predict')

model.eval()
with torch.no_grad():
    predict = model(img)

for i in range(4):
    arr[i][0].imshow(img[i].permute(1, 2, 0));

    arr[i][1].imshow(F.one_hot(mask[i]).float())

    arr[i][2].imshow(F.one_hot(torch.argmax(predict[i], 0).cpu()).float())


# In[61]:


test_transform = Compose([Resize((256, 256), interpolation=InterpolationMode.BILINEAR),
                     PILToTensor()])


# In[62]:


class UNetTestDataClass(Dataset):
    def __init__(self, images_path, transform):
        super(UNetTestDataClass, self).__init__()

        images_list = os.listdir(images_path)
        images_list = [images_path+i for i in images_list]

        self.images_list = images_list
        self.transform = transform

    def __getitem__(self, index):
        img_path = self.images_list[index]
        data = Image.open(img_path)
        h = data.size[1]
        w = data.size[0]
        data = self.transform(data) / 255
        return data, img_path, h, w

    def __len__(self):
        return len(self.images_list)


# In[63]:


path = '/kaggle/input/bkai-igh-neopolyp/test/test/'
unet_test_dataset = UNetTestDataClass(path, test_transform)
test_dataloader = DataLoader(unet_test_dataset, batch_size=8, shuffle=True)


# In[64]:


for i, (data, path, h, w) in enumerate(test_dataloader):
    img = data
    break


# In[65]:


fig, arr = plt.subplots(5, 2, figsize=(16, 12))
arr[0][0].set_title('Image');
arr[0][1].set_title('Predict');

model.eval()
with torch.no_grad():
    predict = model(img)

for i in range(5):
    arr[i][0].imshow(img[i].permute(1, 2, 0));
    arr[i][1].imshow(F.one_hot(torch.argmax(predict[i], 0).cpu()).float())


# In[66]:


model.eval()

output_dir = "/kaggle/working/predicted_masks"  # Use /kaggle/working/ instead of /kaggle/input/
if not os.path.isdir(output_dir):
    os.mkdir(output_dir)

for _, (img, path, H, W) in enumerate(test_dataloader):
    a = path
    b = img
    h = H
    w = W

    with torch.no_grad():
        predicted_mask = model(b)

    for i in range(len(a)):
        image_id = a[i].split('/')[-1].split('.')[0]
        filename = image_id + ".png"
        mask2img = Resize((h[i].item(), w[i].item()), interpolation=InterpolationMode.NEAREST)(ToPILImage()(F.one_hot(torch.argmax(predicted_mask[i], 0)).permute(2, 0, 1).float()))

        mask2img.save(os.path.join(output_dir, filename))


# In[67]:


def rle_to_string(runs):
    return ' '.join(str(x) for x in runs)

def rle_encode_one_mask(mask):
    pixels = mask.flatten()
    pixels[pixels > 0] = 255
    use_padding = False
    if pixels[0] or pixels[-1]:
        use_padding = True
        pixel_padded = np.zeros([len(pixels) + 2], dtype=pixels.dtype)
        pixel_padded[1:-1] = pixels
        pixels = pixel_padded

    rle = np.where(pixels[1:] != pixels[:-1])[0] + 2
    if use_padding:
        rle = rle - 1
    rle[1::2] = rle[1::2] - rle[:-1:2]
    return rle_to_string(rle)

def mask2string(dir):
    ## mask --> string
    strings = []
    ids = []
    ws, hs = [[] for i in range(2)]
    for image_id in os.listdir(dir):
        id = image_id.split('.')[0]
        path = os.path.join(dir, image_id)
        print(path)
        img = cv2.imread(path)[:,:,::-1]
        h, w = img.shape[0], img.shape[1]
        for channel in range(2):
            ws.append(w)
            hs.append(h)
            ids.append(f'{id}_{channel}')
            string = rle_encode_one_mask(img[:,:,channel])
            strings.append(string)
    r = {
        'ids': ids,
        'strings': strings,
    }
    return r


MASK_DIR_PATH = '/kaggle/working/predicted_masks' # change this to the path to your output mask folder
dir = MASK_DIR_PATH
res = mask2string(dir)
df = pd.DataFrame(columns=['Id', 'Expected'])
df['Id'] = res['ids']
df['Expected'] = res['strings']
df.to_csv(r'output.csv', index=False)


# In[ ]:




