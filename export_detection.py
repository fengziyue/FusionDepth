# python export_detection.py --eval_mono --load_weights_folder log/mdp/models/weights_refine74_pal/ --eval_mono  --refine2d_deep true --catxy true --refine_depthnet_with_beam false --refine_2d

from __future__ import absolute_import, division, print_function

import os
import cv2
import tqdm
import numpy as np
import open3d
import torch
from torch.utils.data import DataLoader
import wandb
from layers import disp_to_depth
from utils import readlines
from options import MonodepthOptions
import datasets
import networks
from kitti_util_from_pse import Calibration
from gdc_old import GDC
from layers import *


def get_detec_calib(folder, frame_idx):
    img_path = os.path.join(folder, "image_02/data/{:06d}.png".format(int(frame_idx)))
    imshape = cv2.imread(img_path).shape[:2]
    if imshape == (375, 1242):
        return 'kitti_data/2011_09_26'
    if imshape == (370, 1224):
        return 'kitti_data/2011_09_28'
    if imshape == (374, 1238):
        return 'kitti_data/2011_09_29'
    if imshape == (370, 1226):
        return 'kitti_data/2011_09_30'
    if imshape == (376, 1241):
        return 'kitti_data/2011_10_03'

cv2.setNumThreads(0)  # This speeds up evaluation 5x on our unix systems (OpenCV 3.3.1)


splits_dir = os.path.join(os.path.dirname(__file__), "splits")

# Models which were trained with stereo supervision were trained with a nominal
# baseline of 0.1 units. The KITTI rig has a baseline of 54cm. Therefore,
# to convert our stereo predictions to real-world scale we multiply our depths by 5.4.
STEREO_SCALE_FACTOR = 5.4


def compute_errors(gt, pred):
    """Computation of error metrics between predicted and ground truth depths
    """
    thresh = np.maximum((gt / pred), (pred / gt))
    a1 = (thresh < 1.25     ).mean()
    a2 = (thresh < 1.25 ** 2).mean()
    a3 = (thresh < 1.25 ** 3).mean()

    rmse = (gt - pred) ** 2
    rmse = np.sqrt(rmse.mean())

    rmse_log = (np.log(gt) - np.log(pred)) ** 2
    rmse_log = np.sqrt(rmse_log.mean())

    abs_rel = np.mean(np.abs(gt - pred) / gt)

    sq_rel = np.mean(((gt - pred) ** 2) / gt)

    return abs_rel, sq_rel, rmse, rmse_log, a1, a2, a3


def batch_post_process_disparity(l_disp, r_disp):
    """Apply the disparity post-processing method as introduced in Monodepthv1
    """
    _, h, w = l_disp.shape
    m_disp = 0.5 * (l_disp + r_disp)
    l, _ = np.meshgrid(np.linspace(0, 1, w), np.linspace(0, 1, h))
    l_mask = (1.0 - np.clip(20 * (l - 0.05), 0, 1))[None, ...]
    r_mask = l_mask[:, :, ::-1]
    return r_mask * l_disp + l_mask * r_disp + (1.0 - l_mask - r_mask) * m_disp


def evaluate(opt):
    """Evaluates a pretrained model using a specified test set
    """
    MIN_DEPTH = 1e-3
    MAX_DEPTH = 80
    wandb.init(project="mono-eval")

    assert sum((opt.eval_mono, opt.eval_stereo)) == 1, \
        "Please choose mono or stereo evaluation by setting either --eval_mono or --eval_stereo"

    if opt.ext_disp_to_eval is None:

        opt.load_weights_folder = os.path.expanduser(opt.load_weights_folder)

        assert os.path.isdir(opt.load_weights_folder), \
            "Cannot find a folder at {}".format(opt.load_weights_folder)

        print("-> Loading weights from {}".format(opt.load_weights_folder))

        filenames = readlines(os.path.join(splits_dir, 'detection', "test.txt"))
        encoder_path = os.path.join(opt.load_weights_folder, "encoder.pth")
        decoder_path = os.path.join(opt.load_weights_folder, "depth.pth")

        encoder_dict = torch.load(encoder_path)

        dataset = datasets.KITTIDetecDataset(opt.data_path, filenames,
                                             encoder_dict['height'], encoder_dict['width'],
                                             [0], 4, is_train=False, img_ext='.png', opt=opt)
        if opt.eval_gdc:
            opt.eval_batch_size = 1
        dataloader = DataLoader(dataset, opt.eval_batch_size, shuffle=False, num_workers=opt.num_workers,
                                pin_memory=True, drop_last=False)

        encoder = networks.ResnetEncoder(opt.num_layers, False,
                                         cat4beam_to_color=opt.cat_4beam_to_color,
                                         cat2channel=opt.cat2start)
        depth_decoder = networks.DepthDecoder(encoder.num_ch_enc,
                                              opt.scales, cat2end=opt.cat2end)

        model_dict = encoder.state_dict()
        encoder.load_state_dict({k: v for k, v in encoder_dict.items() if k in model_dict})
        depth_decoder.load_state_dict(torch.load(decoder_path))

        encoder.cuda()
        encoder.eval()
        depth_decoder.cuda()
        depth_decoder.eval()

        if opt.beam_encoder:
            beam_encoder = networks.ResnetEncoder(opt.num_layers, False,
                                                  beam_encoder=True)
            beam_encoder_path = os.path.join(opt.load_weights_folder, "beam_encoder.pth")
            beam_encoder.load_state_dict(torch.load(beam_encoder_path))
            beam_encoder.cuda()
            beam_encoder.eval()
        if opt.refine_2d:
            refine_net = networks.DepthDecoder(encoder.num_ch_enc, opt.scales, road=True,
                                                                    catxy=(opt.catxy == 'true'),
                                                                    deep=(opt.refine2d_deep == 'true'))
            refine_net_path = os.path.join(opt.load_weights_folder, "refine2d_decoder.pth")
            refine_net.load_state_dict(torch.load(refine_net_path))
            refine_net.cuda()
            refine_net.eval()

        pred_disps = []
        if opt.eval_gdc:
            dates = []
        invKs = []
        idx = 0

        catxy = {}
        for scale in opt.scales:
            h = opt.height // (2 ** scale)
            w = opt.width // (2 ** scale)
            if opt.catxy == 'true':
                catxy["True", scale] = Cat_xy(opt.eval_batch_size, h, w)
                catxy["True", scale].cuda()

        print("-> Computing predictions with size {}x{}".format(
            encoder_dict['width'], encoder_dict['height']))

        with torch.no_grad():
            for data in tqdm.tqdm(dataloader):
                if opt.eval_gdc:
                    dates.append(data['date'][0])
                invKs.append(data[("inv_K", 0)])
                input_color = data[("color", 0, 0)].cuda()

                if opt.post_process:
                    # Post-processed results require each image to have two forward passes
                    input_color = torch.cat((input_color, torch.flip(input_color, [3])), 0)

                if opt.cat_4beam_to_color:
                    features = encoder(torch.cat((input_color, data["4beam"].cuda()), 1))
                elif opt.cat2start:
                    features = encoder(torch.cat((input_color, data["2channel"].cuda()), 1))
                else:
                    features = encoder(input_color)

                if opt.cat2end:
                    output = depth_decoder(features, two_channel=data["2channel"].cuda())
                elif opt.beam_encoder:
                    beam_features = beam_encoder(data["2channel"].cuda())
                    if opt.refine_depthnet_with_beam == 'true' or not opt.refine_2d:
                        output = depth_decoder(features, beam_features=beam_features)
                    else:
                        output = depth_decoder(features)
                else:
                    output = depth_decoder(features)


                if opt.refine_2d:
                    for iter in range(opt.refine_iter):
                        beam = data['4beam']
                        two_cha = data['2channel'].cuda()
                        disp_0 = output[("disp", 0)]
                        for scale in opt.scales:
                            if not opt.refine_a0 == 'true':
                                disp = output[("disp", scale)]
                            else:
                                disp = disp_0
                                disp_0 = F.max_pool2d(disp_0, 2, ceil_mode=True)
                            disp640 = F.interpolate(disp, [opt.height, opt.width], mode="bilinear", align_corners=False)
                            _, depth = disp_to_depth(disp640, opt.min_depth, opt.max_depth)

                            mask = beam > 0
                            crop_mask = torch.zeros_like(mask)
                            crop_mask[:, :, 78:190, 23:617] = 1  # 375 1242
                            mask = mask * crop_mask
                            ratio = torch.median(beam[mask] * 100.0) / torch.median(depth[mask]).detach()
                            depth *= ratio

                            scaled_disp = (F.interpolate(1 / depth, disp.shape[2:],
                                                         mode="bilinear", align_corners=False) - 0.01) / 9.9
                            if scale != 0:
                                two_cha = F.max_pool2d(two_cha, 2, ceil_mode=True)
                            if opt.catxy == 'true':
                                for i in range(scale):
                                    depth = F.max_pool2d(depth, 2, ceil_mode=True)
                                xyz = catxy['True', scale](depth, data[("inv_K", scale)].cuda())
                                output[("disp", scale)] = torch.cat([scaled_disp, xyz, two_cha], 1)
                            else:
                                output[("disp", scale)] = torch.cat([scaled_disp, two_cha], 1)

                        refine_output = refine_net(features, beam_features=beam_features,
                                                   depth_maps=output, tanh=opt.refine_offset)
                        for i in opt.scales:
                            output[("disp", i)] = refine_output[("disp", i)]

                output[("disp", 0)] = F.interpolate(
                    output[("disp", 0)], [192, 640], mode="bilinear", align_corners=False)
                pred_disp, _ = disp_to_depth(output[("disp", 0)], opt.min_depth, opt.max_depth)
                pred_disp = pred_disp.cpu()[:, 0].numpy()

                if opt.post_process:
                    N = pred_disp.shape[0] // 2
                    pred_disp = batch_post_process_disparity(pred_disp[:N], pred_disp[N:, :, ::-1])

                pred_disps.append(pred_disp)

                if opt.save_sample == idx:
                    from matplotlib import pyplot as plt
                    plt.pcolor(pred_disp[0], cmap='viridis')
                    plt.axis('equal')
                    plt.savefig('/home/zfeng/Desktop/depth{}.jpg'.format(idx), bbox_inches='tight', pad_inches=0)
                    plt.show()
                if opt.visualize:
                    rgb = data[("color", 0, 0)][0].permute(1, 2, 0).numpy() * 255
                    rgb = cv2.resize(rgb, (1242, 375))
                    rgb = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                    cv2.imwrite('visualization/prediction/{}rgb.jpg'.format(idx),rgb)
                idx += 1

        pred_disps = np.concatenate(pred_disps)

    else:
        # Load predictions from file
        print("-> Loading predictions from {}".format(opt.ext_disp_to_eval))
        pred_disps = np.load(opt.ext_disp_to_eval)

        if opt.eval_eigen_to_benchmark:
            eigen_to_benchmark_ids = np.load(
                os.path.join(splits_dir, "benchmark", "eigen_to_benchmark_ids.npy"))

            pred_disps = pred_disps[eigen_to_benchmark_ids]

    if opt.save_pred_disps:
        output_path = os.path.join(
            opt.load_weights_folder, "disps_{}_split.npy".format(opt.eval_split))
        print("-> Saving predicted disparities to ", output_path)
        np.save(output_path, pred_disps)

    if opt.no_eval:
        print("-> Evaluation disabled. Done.")
        quit()

    elif opt.eval_split == 'benchmark':
        save_dir = os.path.join(opt.load_weights_folder, "benchmark_predictions")
        print("-> Saving out benchmark predictions to {}".format(save_dir))
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        for idx in range(len(pred_disps)):
            disp_resized = cv2.resize(pred_disps[idx], (1216, 352))
            depth = STEREO_SCALE_FACTOR / disp_resized
            depth = np.clip(depth, 0, 80)
            depth = np.uint16(depth * 256)
            save_path = os.path.join(save_dir, "{:010d}.png".format(idx))
            cv2.imwrite(save_path, depth)

        print("-> No ground truth is available for the KITTI benchmark, so not evaluating. Done.")
        quit()

    gt_path = os.path.join(splits_dir, 'detection', "gt_depths.npz")
    gt_depths = np.load(gt_path, fix_imports=True, encoding='latin1', allow_pickle=True)["data"]

    if opt.random_sample == -1:
        beam_path = os.path.join(splits_dir, 'detection', "4beam.npz")
    beam_depths = np.load(beam_path, fix_imports=True, encoding='latin1', allow_pickle=True)["data"]

    print("-> Evaluating")

    if opt.eval_stereo:
        print("   Stereo evaluation - "
              "disabling median scaling, scaling by {}".format(STEREO_SCALE_FACTOR))
        opt.disable_median_scaling = True
        opt.pred_depth_scale_factor = STEREO_SCALE_FACTOR
    else:
        print("   Mono evaluation - using median scaling")

    errors = []
    ratios = []

    #pred_depths = 1 / torch.tensor(pred_disps).unsqueeze(1)
    #pred_depths = torch.clamp(F.interpolate(
    #    pred_depths, [375, 1242], mode="bilinear", align_corners=False), 1e-3, 80).squeeze().numpy()

    for i in tqdm.tqdm(range(pred_disps.shape[0])):
        gt_depth = gt_depths[i]
        beam_depth = beam_depths[i]
        gt_height, gt_width = gt_depth.shape[:2]

        pred_disp = pred_disps[i]
        pred_disp = cv2.resize(pred_disp, (gt_width, gt_height))

        pred_depth = 1 / pred_disp
        #gt_height, gt_width = 375, 1242
        #pred_depth = pred_depths[i]
        #import skimage
        #gt_depth = skimage.transform.resize(
        #    gt_depths[i], (375, 1242), order=0, preserve_range=True, mode='constant')
        if opt.eval_split == "eigen":
            mask = np.logical_and(gt_depth > MIN_DEPTH, gt_depth < MAX_DEPTH)

            crop = np.array([0.40810811 * gt_height, 0.99189189 * gt_height,
                             0.03594771 * gt_width,  0.96405229 * gt_width]).astype(np.int32)
            crop_mask = np.zeros(mask.shape)
            crop_mask[crop[0]:crop[1], crop[2]:crop[3]] = 1
            mask = np.logical_and(mask, crop_mask)

        else:
            mask = gt_depth > 0
        

        pred_depth *= opt.pred_depth_scale_factor
        if not opt.disable_median_scaling:
            ratio = np.median(gt_depth[mask]) / np.median(pred_depth[mask])
            ratios.append(ratio)
            pred_depth *= ratio

        # cv2.imwrite('gt.jpg', gt_depth)
        # cv2.imwrite('pred_gdc.jpg', pred_depth)
        # cv2.imwrite('4beam.jpg', beam_depth)
        # gtd = beam_depth.copy()
        # gtd[gtd==0] = -1
        # bpd = BackprojectDepth(1, gt_height, gt_width)
        # pred_points = bpd(torch.tensor(pred_depth), invKs[i]).squeeze()[:3,].transpose(0,1).numpy()
        # beam_points = bpd(torch.tensor(gtd), invKs[i]).squeeze()[:3,].transpose(0,1).numpy()


        if opt.eval_gdc:
            try:
                calib_path = get_detec_calib('kitti_data/kitti_detect/training', i)
                calib_path += '/calib_cam_to_cam.txt'
                calib = Calibration(calib_path)
                gtd = beam_depth
                gtd[gtd==0] = -1
                if opt.random_sample == -1:
                    consider_range = (-0.1, 4.0)
                else:
                    consider_range = (-1.5, 9)

                corrected = GDC(pred_depth, gtd, calib, W_tol=3e-5, recon_tol=5e-4,
                                k=10, method='cg', verbose=False, consider_range=consider_range)
                pred_depth = corrected
                    #import pdb
                    #pdb.set_trace()
            except:
                print("GDC failed")

        if opt.visualize:
            diff = abs(pred_depth - gt_depth)
            diff = np.ones_like(diff) * 80 - np.clip(diff, 0, 2) * 40
            diff_color = cv2.applyColorMap(diff.astype(np.uint8), cv2.COLORMAP_HSV)
            ones = np.ones_like(diff_color) * 255
            ones[mask] = diff_color[mask]
            cv2.imwrite('visualization/prediction/{}{}.jpg'.format(i, opt.vis_name), ones)

        pred_save = (pred_depth * 256.0).astype(np.uint16)
        save_folder = 'kitti_data/kitti_detect/training/{}'.format(opt.det_name)
        if not os.path.exists(save_folder):
            os.mkdir(save_folder)
        cv2.imwrite(save_folder + '/{:06d}.png'.format(i), pred_save)

        pred_depth = pred_depth[mask]
        gt_depth = gt_depth[mask]

        pred_depth[pred_depth < MIN_DEPTH] = MIN_DEPTH
        pred_depth[pred_depth > MAX_DEPTH] = MAX_DEPTH

        #print(pred_depth)
        #print(gt_depth.asdf)
        errors.append(compute_errors(gt_depth, pred_depth))

    if not opt.disable_median_scaling:
        ratios = np.array(ratios)
        med = np.median(ratios)
        print(" Scaling ratios | med: {:0.3f} | std: {:0.3f}".format(med, np.std(ratios / med)))

    mean_errors = np.array(errors).mean(0)
    print(mean_errors[0])
    print("\n  " + ("{:>8} | " * 7).format("abs_rel", "sq_rel", "rmse", "rmse_log", "a1", "a2", "a3"))
    print(("&{: 8.3f}  " * 7).format(*mean_errors.tolist()) + "\\\\")
    print("\n-> Done!")


if __name__ == "__main__":
    options = MonodepthOptions()
    evaluate(options.parse())
