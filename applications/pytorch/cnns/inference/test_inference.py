# Copyright (c) 2020 Graphcore Ltd. All rights reserved.
import os
import subprocess
import pytest
import poptorch
import torch
import torchvision
from PIL import Image
from pathlib import Path
import sys
sys.path.append('..')
import models
import datasets


def run_inference(parameters, poprun_parameters=""):
    cwd = os.path.dirname(os.path.abspath(__file__))
    param_list = parameters.split(" ")
    cmd = ["python3", 'run_benchmark.py'] + param_list
    if poprun_parameters != "":
        poprun_param_list = ["poprun"] + poprun_parameters.split(" ")
        cmd = poprun_param_list + cmd
    out = subprocess.check_output(cmd, cwd=cwd, stderr=subprocess.PIPE).decode("utf-8")
    return out


def get_max_thoughput(output):
    if output.find("Throughput at") != -1:
        pos = output.find("max=")
        return float(output[pos+4:].split(',')[0][:-7])
    else:
        return -1


class TestInference:
    @pytest.mark.category2
    @pytest.mark.ipus(1)
    def test_real_data(self):
        out = run_inference("--data real --model resnet18 --precision 16.16 --iterations 10 --dataloader-worker 4")
        max_throughput = get_max_thoughput(out)
        assert max_throughput > 0

    @pytest.mark.category2
    @pytest.mark.ipus(2)
    def test_replicate(self):
        out = run_inference("--data synthetic --replicas 2 --model resnet18 --precision 16.16 --iterations 10 --dataloader-worker 4")
        max_throughput = get_max_thoughput(out)
        assert max_throughput > 0

    @pytest.mark.category2
    @pytest.mark.ipus(2)
    def test_syntetic_pipeline(self):
        out = run_inference("--data synthetic --batch-size 2 --model resnet18 --pipeline-splits layer3/0 --device-iterations 4 --precision 16.16 --iterations 10 --dataloader-worker 4")
        max_throughput = get_max_thoughput(out)
        assert max_throughput > 0

    @pytest.mark.category2
    @pytest.mark.ipus(2)
    def test_realdata_pipeline(self):
        out = run_inference("--data real --batch-size 2 --model resnet18 --pipeline-splits layer3/0 --device-iterations 4 --precision 16.16 --iterations 10 --dataloader-worker 4")
        max_throughput = get_max_thoughput(out)
        assert max_throughput > 0


    @pytest.mark.category2
    @pytest.mark.ipus(1)
    def test_full_precision(self):
        out = run_inference(f"--data synthetic --model resnet18 --batch-size 1 --precision 32.32 --iterations 10 --dataloader-worker 4")
        max_thoughput = get_max_thoughput(out)
        assert max_thoughput > 0


    @pytest.mark.category2
    @pytest.mark.ipus(2)
    def test_poprun_inference(self):
        out = run_inference(f"--data generated --model resnet18 --batch-size 4 --precision 16.16 --iterations 10 --dataloader-worker 4", "--mpi-global-args='--allow-run-as-root' --num-instances=2 --numa-aware=yes --num-replicas=2")
        max_thoughput = get_max_thoughput(out)
        assert max_thoughput > 0


@pytest.mark.category2
@pytest.mark.ipus(1)
@pytest.mark.parametrize("model_name", ["resnet34", "resnet50", "resnext50", "mobilenet", "efficientnet-b0", "efficientnet-b1", "efficientnet-b2", "efficientnet-b3", "efficientnet-b4"])
def test_single_ipu_models(model_name):
    out = run_inference(f"--data synthetic --batch-size 1 --model {model_name} --iterations 10 --precision 16.16 --dataloader-worker 4")
    max_throughput = get_max_thoughput(out)
    assert max_throughput > 0


@pytest.mark.category2
@pytest.mark.ipus(1)
@pytest.mark.parametrize("norm_layer", ["group", "none"])
def test_normlayer_resnet(norm_layer):
    out = run_inference(f"--data synthetic --batch-size 1 --model resnet18 --norm-type {norm_layer} --iterations 10 --precision 16.16 --dataloader-worker 4")
    max_thoughput = get_max_thoughput(out)
    assert max_thoughput > 0


@pytest.mark.category2
@pytest.mark.ipus(1)
@pytest.mark.parametrize("norm_layer", ["group", "none"])
def test_normlayer_efficientnet(norm_layer):
    out = run_inference(f"--data synthetic --batch-size 1 --model efficientnet-b0 --norm-type {norm_layer} --norm-num-groups 4 --iterations 10 --precision 16.16 --dataloader-worker 4")
    max_thoughput = get_max_thoughput(out)
    assert max_thoughput > 0


@pytest.mark.category2
@pytest.mark.ipus(1)
@pytest.mark.parametrize("precision", ["16.16", "32.32"])
@pytest.mark.parametrize("model_name", ["resnet18", "resnet50", "efficientnet-b0", "efficientnet-b4"])
def test_pretrained_prediction(precision, model_name):
    ground_truth = [('zebra.jpg', 340), ('jay.jpg', 17), ('polar_bear.jpg', 296), ('banana.jpg', 954),
                    ('hippo.jpg', 344), ('ostrich.jpg', 9), ('ping-pong_ball.jpg', 722), ('pelican.jpg', 144)]

    class HelperClass:
        def __init__(self):
            pass
    opts = HelperClass()
    opts.model = model_name
    opts.data = "imagenet"
    opts.norm_type = "batch"
    opts.batchnorm_momentum = 0.1
    opts.pipeline_splits = []
    opts.normalization_location = "host"
    opts.precision = precision
    opts.full_precision_norm = False
    opts.efficientnet_expand_ratio = 6
    opts.efficientnet_group_dim = 1
    model = models.get_model(opts, datasets.datasets_info[opts.data], pretrained=True)
    model.eval()
    model_opts = poptorch.Options()
    if precision == "16.16":
        model_opts.Precision.setPartialsType(torch.float16)
    else:
        model_opts.Precision.setPartialsType(torch.float32)

    poptorch_model = poptorch.inferenceModel(model, model_opts)
    augment = datasets.get_preprocessing_pipeline(train=False, half_precision=True if precision == "16.16" else False)
    for img_name, class_id in ground_truth:
        sample = augment(Image.open(os.path.join(Path(__file__).parent.parent.absolute(), "data/images/", img_name))).view(1, 3, 224, 224)
        pred = poptorch_model(sample)
        assert class_id == torch.argmax(pred), f"Prediction for {img_name} was incorrect."


@pytest.mark.category1
@pytest.mark.ipus(1)
def test_pretrained_batchnorm_fp16():
    fake_data = torch.ones(1, 64, 10, 10)
    model = torchvision.models.resnet18(pretrained=True).bn1   # Get a batchnorm layer from a real world model.
    cpu_mean = model.running_mean
    cpu_var = model.running_var
    model.half()
    model_opts = poptorch.Options()
    model_opts.anchorTensor('running_mean', 'running_mean')
    model_opts.anchorTensor('running_var', 'running_var')
    model_opts.Precision.runningStatisticsAlwaysFloat(True)

    poptorch_model = poptorch.inferenceModel(model, model_opts)
    output = poptorch_model(fake_data)   # Compile the model.
    ipu_mean = poptorch_model.getAnchoredTensor('running_mean')
    ipu_var = poptorch_model.getAnchoredTensor('running_var')

    assert torch.allclose(ipu_mean, cpu_mean.half())
    assert torch.allclose(ipu_var, cpu_var.half())
