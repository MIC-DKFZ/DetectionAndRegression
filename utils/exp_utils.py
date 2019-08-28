#!/usr/bin/env python
# Copyright 2019 Division of Medical Image Computing, German Cancer Research Center (DKFZ).
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
#import plotting as plg

import sys
import os
import subprocess
import threading
import pickle
import importlib.util
import psutil
from functools import partial
import time

import logging
from tensorboardX import SummaryWriter

from collections import OrderedDict
import numpy as np
import pandas as pd
import torch


def import_module(name, path):
    """
    correct way of importing a module dynamically in python 3.
    :param name: name given to module instance.
    :param path: path to module.
    :return: module: returned module instance.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def save_obj(obj, name):
    """Pickle a python object."""
    with open(name + '.pkl', 'wb') as f:
        pickle.dump(obj, f, pickle.HIGHEST_PROTOCOL)

def load_obj(file_path):
    with open(file_path, 'rb') as handle:
        return pickle.load(handle)

def IO_safe(func, *args, _tries=5, _raise=True, **kwargs):
    """ Wrapper calling function func with arguments args and keyword arguments kwargs to catch input/output errors
        on cluster.
    :param func: function to execute (intended to be read/write operation to a problematic cluster drive, but can be
        any function).
    :param args: positional args of func.
    :param kwargs: kw args of func.
    :param _tries: how many attempts to make executing func.
    """
    for _try in range(_tries):
        try:
            return func(*args, **kwargs)
        except OSError as e:  # to catch cluster issues with network drives
            if _raise:
                raise e
            else:
                print("After attempting execution {} time{}, following error occurred:\n{}".format(_try+1,"" if _try==0 else "s", e))
                continue


def query_nvidia_gpu(device_id, d_keyword=None, no_units=False):
    """
    :param device_id:
    :param d_keyword: -d, --display argument (keyword(s) for selective display), all are selected if None
    :return: dict of gpu-info items
    """
    cmd = ['nvidia-smi', '-i', str(device_id), '-q']
    if d_keyword is not None:
        cmd += ['-d', d_keyword]
    outp = subprocess.check_output(cmd).strip().decode('utf-8').split("\n")
    outp = [x for x in outp if len(x)>0]
    headers = [ix for ix, item in enumerate(outp) if len(item.split(":"))==1] + [len(outp)]

    out_dict = {}
    for lix, hix in enumerate(headers[:-1]):
        head = outp[hix].strip().replace(" ", "_").lower()
        out_dict[head] = {}
        for lix2 in range(hix, headers[lix+1]):
            try:
                key, val = [x.strip().lower() for x in outp[lix2].split(":")]
                if no_units:
                    val = val.split()[0]
                out_dict[head][key] = val
            except:
                pass

    return out_dict

class CombinedPrinter(object):
    """combined print function.
    prints to logger and/or file if given, to normal print if non given.

    """
    def __init__(self, logger=None, file=None):

        if logger is None and file is None:
            self.out = [print]
        elif logger is None:
            self.out = [file.write]
        elif file is None:
            self.out = [logger.info]
        else:
            self.out = [logger.info, file.write]

    def __call__(self, string):
        for fct in self.out:
            fct(string)

class Nvidia_GPU_Logger(object):
    def __init__(self):
        self.count = None

    def get_vals(self):

        cmd = ['nvidia-settings', '-t', '-q', 'GPUUtilization']
        gpu_util = subprocess.check_output(cmd).strip().decode('utf-8').split(",")
        gpu_util = dict([f.strip().split("=") for f in gpu_util])
        cmd[-1] = 'UsedDedicatedGPUMemory'
        gpu_used_mem = subprocess.check_output(cmd).strip().decode('utf-8')
        current_vals = {"gpu_mem_alloc": gpu_used_mem, "gpu_graphics_util": int(gpu_util['graphics']),
                             "gpu_mem_util": gpu_util['memory'], "time": time.time()}
        return current_vals

    def loop(self):
        i = 0
        while True:
            self.get_vals()
            self.log["time"].append(time.time())
            self.log["gpu_util"].append(self.current_vals["gpu_graphics_util"])
            if self.count != None:
                i += 1
                if i == count:
                    exit(0)
            time.sleep(self.interval)

    def start(self, interval=1.):
        self.interval = interval
        self.start_time = time.time()
        self.log = {"time": [], "gpu_util": []}
        if self.interval is not None:
            thread = threading.Thread(target=self.loop)
            thread.daemon = True
            thread.start()

class CombinedLogger(object):
    """Combine console and tensorboard logger and record system metrics.
    """
    def __init__(self, name, log_dir, server_env=True, fold="", sysmetrics_interval=2):
        self.pylogger = logging.getLogger(name)
        self.tboard = SummaryWriter(log_dir=log_dir)
        self.times = {}
        self.fold = fold
        # monitor system metrics (cpu, mem, ...)
        if not server_env:
            self.sysmetrics = pd.DataFrame(columns=["global_step", "rel_time", r"CPU (%)", "mem_used (GB)", r"mem_used (%)",
                                                    r"swap_used (GB)", r"gpu_utilization (%)"], dtype="float16")
            for device in range(torch.cuda.device_count()):
                self.sysmetrics["mem_allocd (GB) by torch on {:10s}".format(torch.cuda.get_device_name(device))] = np.nan
                self.sysmetrics["mem_cached (GB) by torch on {:10s}".format(torch.cuda.get_device_name(device))] = np.nan
            self.sysmetrics_start(sysmetrics_interval)

    def __getattr__(self, attr):
        """delegate all undefined method requests to objects of
        this class in order pylogger, tboard (first find first serve).
        E.g., combinedlogger.add_scalars(...) should trigger self.tboard.add_scalars(...)
        """
        for obj in [self.pylogger, self.tboard]:
            if attr in dir(obj):
                return getattr(obj, attr)
        raise AttributeError("CombinedLogger has no attribute {}".format(attr))


    def time(self, name, toggle=None):
        """record time-spans as with a stopwatch.
        :param name:
        :param toggle: True^=On: start time recording, False^=Off: halt rec. if None determine from current status.
        :return: either start-time or last recorded interval
        """
        if toggle is None:
            if name in self.times.keys():
                toggle = not self.times[name]["toggle"]
            else:
                toggle = True

        if toggle:
            if not name in self.times.keys():
                self.times[name] = {"total": 0, "last":0}
            elif self.times[name]["toggle"] == toggle:
                print("restarting running stopwatch")
            self.times[name]["last"] = time.time()
            self.times[name]["toggle"] = toggle
            return time.time()
        else:
            if toggle == self.times[name]["toggle"]:
                self.info("WARNING: tried to stop stopped stop watch: {}.".format(name))
            self.times[name]["last"] = time.time()-self.times[name]["last"]
            self.times[name]["total"] += self.times[name]["last"]
            self.times[name]["toggle"] = toggle
            return self.times[name]["last"]

    def get_time(self, name=None, kind="total", format=None, reset=False):
        """
        :param name:
        :param kind: 'total' or 'last'
        :param format: None for float, "hms"/"ms" for (hours), mins, secs as string
        :param reset: reset time after retrieving
        :return:
        """
        if name is None:
            times = self.times
            if reset:
                self.reset_time()
            return times

        else:
            time = self.times[name][kind]
            if format == "hms":
                m, s = divmod(time, 60)
                h, m = divmod(m, 60)
                time = "{:d}h:{:02d}m:{:02d}s".format(int(h), int(m), int(s))
            elif format == "ms":
                m, s = divmod(time, 60)
                time = "{:02d}m:{:02d}s".format(int(m), int(s))
            if reset:
                self.reset_time(name)
            return time

    def reset_time(self, name=None):
        if name is None:
            self.times = {}
        else:
            del self.times[name]


    def sysmetrics_update(self, global_step=None):
        if global_step is None:
            global_step = time.strftime("%x_%X")
        mem = psutil.virtual_memory()     
        mem_used = (mem.total-mem.available)
        gpu_vals = self.gpu_logger.get_vals()
        rel_time = time.time()-self.sysmetrics_start_time
        self.sysmetrics.loc[len(self.sysmetrics)] = [global_step, rel_time,
                            psutil.cpu_percent(), mem_used/1024**3, mem_used/mem.total*100,
                            psutil.swap_memory().used/1024**3, int(gpu_vals['gpu_graphics_util']),
                            *[torch.cuda.memory_allocated(d)/1024**3 for d in range(torch.cuda.device_count())],
                            *[torch.cuda.memory_cached(d)/1024**3 for d in range(torch.cuda.device_count())]
                            ]
        return self.sysmetrics.loc[len(self.sysmetrics)-1].to_dict()

    def sysmetrics2tboard(self, metrics=None, global_step=None, suptitle=None):
        tag = "per_time"
        if metrics is None:
            metrics = self.sysmetrics_update(global_step=global_step)
            tag = "per_epoch"

        if suptitle is not None:
            suptitle = str(suptitle)
        elif self.fold!="":
            suptitle = "Fold_"+str(self.fold)
        if suptitle is not None:
            self.tboard.add_scalars(suptitle+"/System_Metrics/"+tag, {k:v for (k,v) in metrics.items() if (k!="global_step"
                                                        and k!="rel_time")}, global_step)

    def sysmetrics_loop(self):
        try:
            os.nice(-19)
        except:
            print("System-metrics logging has no superior process priority.")
        while True:
            metrics = self.sysmetrics_update()
            self.sysmetrics2tboard(metrics, global_step=metrics["rel_time"])
            #print("thread alive", self.thread.is_alive())
            time.sleep(self.sysmetrics_interval)
            
    def sysmetrics_start(self, interval):
        if interval is not None:
            self.sysmetrics_interval = interval
            self.gpu_logger = Nvidia_GPU_Logger()
            self.sysmetrics_start_time = time.time()
            self.thread = threading.Thread(target=self.sysmetrics_loop)
            self.thread.daemon = True
            self.thread.start()

    def sysmetrics_save(self, out_file):

        self.sysmetrics.to_pickle(out_file)


    def metrics2tboard(self, metrics, global_step=None, suptitle=None):
        """
        :param metrics: {'train': dataframe, 'val':df}, df as produced in
            evaluator.py.evaluate_predictions
        """
        #print("metrics", metrics)
        if global_step is None:
            global_step = len(metrics['train'][list(metrics['train'].keys())[0]])-1
        if suptitle is not None:
            suptitle = str(suptitle)
        else:
            suptitle = "Fold_"+str(self.fold)

        for key in ['train', 'val']:
            #series = {k:np.array(v[-1]) for (k,v) in metrics[key].items() if not np.isnan(v[-1]) and not 'Bin_Stats' in k}
            loss_series = {}
            unc_series = {}
            bin_stat_series = {}
            mon_met_series = {}
            for tag,val in metrics[key].items():
                val = val[-1] #maybe remove list wrapping, recording in evaluator?
                if 'bin_stats' in tag.lower() and not np.isnan(val):
                    bin_stat_series["{}".format(tag.split("/")[-1])] = val
                elif 'uncertainty' in tag.lower() and not np.isnan(val):
                    unc_series["{}".format(tag)] = val
                elif 'loss' in tag.lower() and not np.isnan(val):
                    loss_series["{}".format(tag)] = val
                elif not np.isnan(val):
                    mon_met_series["{}".format(tag)] = val

            self.tboard.add_scalars(suptitle+"/Binary_Statistics/{}".format(key), bin_stat_series, global_step)
            self.tboard.add_scalars(suptitle + "/Uncertainties/{}".format(key), unc_series, global_step)
            self.tboard.add_scalars(suptitle + "/Losses/{}".format(key), loss_series, global_step)
            self.tboard.add_scalars(suptitle+"/Monitor_Metrics/{}".format(key), mon_met_series, global_step)
        self.tboard.add_scalars(suptitle + "/Learning_Rate", metrics["lr"], global_step)
        return
      
    def batchImgs2tboard(self, batch, results_dict, cmap, boxtype2color, img_bg=False, global_step=None):
        raise NotImplementedError("not up-to-date, problem with importing plotting-file, torchvision dependency.")
        if len(batch["seg"].shape)==5: #3D imgs
            slice_ix = np.random.randint(batch["seg"].shape[-1])
            seg_gt = plg.to_rgb(batch['seg'][:,0,:,:,slice_ix], cmap)
            seg_pred = plg.to_rgb(results_dict['seg_preds'][:,0,:,:,slice_ix], cmap)
            
            mod_img = plg.mod_to_rgb(batch["data"][:,0,:,:,slice_ix]) if img_bg else None
            
        elif len(batch["seg"].shape)==4:
            seg_gt = plg.to_rgb(batch['seg'][:,0,:,:], cmap)
            seg_pred = plg.to_rgb(results_dict['seg_preds'][:,0,:,:], cmap)
            mod_img = plg.mod_to_rgb(batch["data"][:,0]) if img_bg else None
        else:
            raise Exception("batch content has wrong format: {}".format(batch["seg"].shape))
        
        #from here on only works in 2D
        seg_gt = np.transpose(seg_gt, axes=(0,3,1,2)) #previous shp: b,x,y,c
        seg_pred = np.transpose(seg_pred, axes=(0,3,1,2))
        
        
        seg = np.concatenate((seg_gt, seg_pred), axis=0)
        # todo replace torchvision (tv) dependency
        seg = tv.utils.make_grid(torch.from_numpy(seg), nrow=2)
        self.tboard.add_image("Batch seg, 1st col: gt, 2nd: pred.", seg, global_step=global_step)      
        
        if img_bg:
            bg_img  = np.transpose(mod_img, axes=(0,3,1,2))
        else:
            bg_img = seg_gt
        box_imgs = plg.draw_boxes_into_batch(bg_img, results_dict["boxes"], boxtype2color)
        box_imgs = tv.utils.make_grid(torch.from_numpy(box_imgs), nrow=4)
        self.tboard.add_image("Batch bboxes", box_imgs, global_step=global_step)
        
        return

    def __del__(self): # otherwise might produce multiple prints e.g. in ipython console
        for hdlr in self.pylogger.handlers:
            hdlr.close()
        self.tboard.close()
        self.pylogger.handlers = []
        del self.pylogger

def get_logger(exp_dir, server_env=False, sysmetrics_interval=2):
    log_dir = os.path.join(exp_dir, "logs")
    logger = CombinedLogger('medical_detection',  os.path.join(log_dir, "tboard"), server_env=server_env,
                            sysmetrics_interval=sysmetrics_interval)
    logger.setLevel(logging.DEBUG)
    log_file = os.path.join(log_dir, 'exec.log')

    logger.addHandler(logging.FileHandler(log_file))
    if not server_env:
        logger.addHandler(ColorHandler())
    else:
        logger.addHandler(logging.StreamHandler())
    logger.pylogger.propagate = False
    print('Logging to {}'.format(log_file))

    return logger

def prep_exp(dataset_path, exp_path, server_env, use_stored_settings=True, is_training=True):
    """
    I/O handling, creating of experiment folder structure. Also creates a snapshot of configs/model scripts and copies them to the exp_dir.
    This way the exp_dir contains all info needed to conduct an experiment, independent to changes in actual source code. Thus, training/inference of this experiment can be started at anytime.
    Therefore, the model script is copied back to the source code dir as tmp_model (tmp_backbone).
    Provides robust structure for cloud deployment.
    :param dataset_path: path to source code for specific data set. (e.g. medicaldetectiontoolkit/lidc_exp)
    :param exp_path: path to experiment directory.
    :param server_env: boolean flag. pass to configs script for cloud deployment.
    :param use_stored_settings: boolean flag. When starting training: If True, starts training from snapshot in existing
        experiment directory, else creates experiment directory on the fly using configs/model scripts from source code.
    :param is_training: boolean flag. distinguishes train vs. inference mode.
    :return: configs object.
    """

    if is_training:

        if use_stored_settings:
            cf_file = import_module('cf', os.path.join(exp_path, 'configs.py'))
            cf = cf_file.Configs(server_env)
            # in this mode, previously saved model and backbone need to be found in exp dir.
            if not os.path.isfile(os.path.join(exp_path, 'model.py')) or \
                    not os.path.isfile(os.path.join(exp_path, 'backbone.py')):
                raise Exception("Selected use_stored_settings option but no model and/or backbone source files exist in exp dir.")
            cf.model_path = os.path.join(exp_path, 'model.py')
            cf.backbone_path = os.path.join(exp_path, 'backbone.py')
        else: # this case overwrites settings files in exp dir, i.e., default_configs, configs, backbone, model
            if not os.path.exists(exp_path):
                os.mkdir(exp_path)
            # run training with source code info and copy snapshot of model to exp_dir for later testing (overwrite scripts if exp_dir already exists.)
            subprocess.call('cp {} {}'.format('default_configs.py', os.path.join(exp_path, 'default_configs.py')), shell=True)
            subprocess.call('cp {} {}'.format(os.path.join(dataset_path, 'configs.py'), os.path.join(exp_path, 'configs.py')), shell=True)
            cf_file = import_module('cf_file', os.path.join(dataset_path, 'configs.py'))
            cf = cf_file.Configs(server_env)
            subprocess.call('cp {} {}'.format(cf.model_path, os.path.join(exp_path, 'model.py')), shell=True)
            subprocess.call('cp {} {}'.format(cf.backbone_path, os.path.join(exp_path, 'backbone.py')), shell=True)
            if os.path.isfile(os.path.join(exp_path, "fold_ids.pickle")):
                subprocess.call('rm {}'.format(os.path.join(exp_path, "fold_ids.pickle")), shell=True)

    else: # testing, use model and backbone stored in exp dir.
        cf_file = import_module('cf', os.path.join(exp_path, 'configs.py'))
        cf = cf_file.Configs(server_env)
        cf.model_path = os.path.join(exp_path, 'model.py')
        cf.backbone_path = os.path.join(exp_path, 'backbone.py')

    cf.exp_dir = exp_path
    cf.test_dir = os.path.join(cf.exp_dir, 'test')
    cf.plot_dir = os.path.join(cf.exp_dir, 'plots')
    if not os.path.exists(cf.test_dir):
        os.mkdir(cf.test_dir)
    if not os.path.exists(cf.plot_dir):
        os.mkdir(cf.plot_dir)
    cf.experiment_name = exp_path.split("/")[-1]
    cf.dataset_name = dataset_path
    cf.server_env = server_env
    cf.created_fold_id_pickle = False

    return cf

class ModelSelector:
    '''
    saves a checkpoint after each epoch as 'last_state' (can be loaded to continue interrupted training).
    saves the top-k (k=cf.save_n_models) ranked epochs. In inference, predictions of multiple epochs can be ensembled
    to improve performance.
    '''

    def __init__(self, cf, logger):

        self.cf = cf
        self.saved_epochs = [-1] * cf.save_n_models
        self.logger = logger


    def run_model_selection(self, net, optimizer, monitor_metrics, epoch):
        """rank epoch via weighted mean from self.cf.model_selection_criteria: {criterion : weight}
        :param net:
        :param optimizer:
        :param monitor_metrics:
        :param epoch:
        :return:
        """
        crita = self.cf.model_selection_criteria #shorter alias

        non_nan_scores = {}
        for criterion in crita.keys():
            #exclude first entry bc its dummy None entry
            non_nan_scores[criterion] = [0 if (ii is None or np.isnan(ii)) else ii for ii in monitor_metrics['val'][criterion]][1:]
            n_epochs = len(non_nan_scores[criterion])
        epochs_scores = []
        for e_ix in range(n_epochs):
            epochs_scores.append(np.sum([weight * non_nan_scores[criterion][e_ix] for
                                         criterion,weight in crita.items()])/len(crita.keys()))

        # ranking of epochs according to model_selection_criterion
        epoch_ranking = np.argsort(epochs_scores)[::-1] + 1 #epochs start at 1

        # if set in configs, epochs < min_save_thresh are discarded from saving process.
        epoch_ranking = epoch_ranking[epoch_ranking >= self.cf.min_save_thresh]

        # check if current epoch is among the top-k epchs.
        if epoch in epoch_ranking[:self.cf.save_n_models]:
            if self.cf.server_env:
                IO_safe(torch.save, net.state_dict(), os.path.join(self.cf.fold_dir, '{}_best_params.pth'.format(epoch)))
                # save epoch_ranking to keep info for inference.
                IO_safe(np.save, os.path.join(self.cf.fold_dir, 'epoch_ranking'), epoch_ranking[:self.cf.save_n_models])
            else:
                torch.save(net.state_dict(), os.path.join(self.cf.fold_dir, '{}_best_params.pth'.format(epoch)))
                np.save(os.path.join(self.cf.fold_dir, 'epoch_ranking'), epoch_ranking[:self.cf.save_n_models])
            self.logger.info(
                "saving current epoch {} at rank {}".format(epoch, np.argwhere(epoch_ranking == epoch)))
            # delete params of the epoch that just fell out of the top-k epochs.
            for se in [int(ii.split('_')[0]) for ii in os.listdir(self.cf.fold_dir) if 'best_params' in ii]:
                if se in epoch_ranking[self.cf.save_n_models:]:
                    subprocess.call('rm {}'.format(os.path.join(self.cf.fold_dir, '{}_best_params.pth'.format(se))),
                                    shell=True)
                    self.logger.info('deleting epoch {} at rank {}'.format(se, np.argwhere(epoch_ranking == se)))

        state = {
            'epoch': epoch,
            'state_dict': net.state_dict(),
            'optimizer': optimizer.state_dict(),
        }

        if self.cf.server_env:
            IO_safe(torch.save, state, os.path.join(self.cf.fold_dir, 'last_state.pth'))
        else:
            torch.save(state, os.path.join(self.cf.fold_dir, 'last_state.pth'))


def load_checkpoint(checkpoint_path, net, optimizer):

    checkpoint = torch.load(checkpoint_path)
    net.load_state_dict(checkpoint['state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer'])
    return checkpoint['epoch']


def prepare_monitoring(cf):
    """
    creates dictionaries, where train/val metrics are stored.
    """
    metrics = {}
    # first entry for loss dict accounts for epoch starting at 1.
    metrics['train'] = OrderedDict()# [(l_name, [np.nan]) for l_name in cf.losses_to_monitor] )
    metrics['val'] = OrderedDict()# [(l_name, [np.nan]) for l_name in cf.losses_to_monitor] )
    metric_classes = []
    if 'rois' in cf.report_score_level:
        metric_classes.extend([v for k, v in cf.class_dict.items()])
        if hasattr(cf, "eval_bins_separately") and cf.eval_bins_separately:
            metric_classes.extend([v for k, v in cf.bin_dict.items()])
    if 'patient' in cf.report_score_level:
        metric_classes.extend(['patient_'+cf.class_dict[cf.patient_class_of_interest]])
        if hasattr(cf, "eval_bins_separately") and cf.eval_bins_separately:
            metric_classes.extend(['patient_' + cf.bin_dict[cf.patient_bin_of_interest]])
    for cl in metric_classes:
        for m in cf.metrics:
            metrics['train'][cl + '_' + m] = [np.nan]
            metrics['val'][cl + '_' + m] = [np.nan]

    return metrics


class _AnsiColorizer(object):
    """
    A colorizer is an object that loosely wraps around a stream, allowing
    callers to write text to the stream in a particular color.

    Colorizer classes must implement C{supported()} and C{write(text, color)}.
    """
    _colors = dict(black=30, red=31, green=32, yellow=33,
                   blue=34, magenta=35, cyan=36, white=37, default=39)

    def __init__(self, stream):
        self.stream = stream

    @classmethod
    def supported(cls, stream=sys.stdout):
        """
        A class method that returns True if the current platform supports
        coloring terminal output using this method. Returns False otherwise.
        """
        if not stream.isatty():
            return False  # auto color only on TTYs
        try:
            import curses
        except ImportError:
            return False
        else:
            try:
                try:
                    return curses.tigetnum("colors") > 2
                except curses.error:
                    curses.setupterm()
                    return curses.tigetnum("colors") > 2
            except:
                raise
                # guess false in case of error
                return False

    def write(self, text, color):
        """
        Write the given text to the stream in the given color.

        @param text: Text to be written to the stream.

        @param color: A string label for a color. e.g. 'red', 'white'.
        """
        color = self._colors[color]
        self.stream.write('\x1b[%sm%s\x1b[0m' % (color, text))

class ColorHandler(logging.StreamHandler):


    def __init__(self, stream=sys.stdout):
        super(ColorHandler, self).__init__(_AnsiColorizer(stream))

    def emit(self, record):
        msg_colors = {
            logging.DEBUG: "green",
            logging.INFO: "default",
            logging.WARNING: "red",
            logging.ERROR: "red"
        }
        color = msg_colors.get(record.levelno, "blue")
        self.stream.write(record.msg + "\n", color)



