#!/usr/bin/env python
# -*- coding: UTF-8 -*-
#
# Copyright 2022-2023 NXP
#
# SPDX-License-Identifier: BSD-3-Clause
"""Test IEE part of nxpimage app."""
import os
import shutil

import pytest
import yaml

from spsdk.apps import nxpimage
from spsdk.utils.misc import load_binary, load_configuration, use_working_directory
from tests.cli_runner import CliRunner

INPUT_BINARY = "evkmimxrt1170_iled_blinky_cm7_QSPI_FLASH_bootable_nopadding.bin"


@pytest.mark.parametrize(
    "case, config, reference, keyblobs",
    [
        (
            "aes_xts512",
            "iee_config.yaml",
            "evkmimxrt1170_iled_blinky_cm7_QSPI_FLASH_nopadding.bin",
            "iee_keyblobs.bin",
        ),
        (
            "aes_xts256",
            "iee_config.yaml",
            "evkmimxrt1170_iled_blinky_cm7_QSPI_FLASH_nopadding.bin",
            "iee_keyblobs.bin",
        ),
        (
            "aes_ctr256",
            "iee_config.yaml",
            "evkmimxrt1170_iled_blinky_cm7_QSPI_FLASH_nopadding.bin",
            "iee_keyblobs.bin",
        ),
        (
            "aes_ctr128",
            "iee_config.yaml",
            "evkmimxrt1170_iled_blinky_cm7_QSPI_FLASH_nopadding.bin",
            "iee_keyblobs.bin",
        ),
        (
            "aes_xts512_multiple",
            "iee_config.yaml",
            "encrypted_blobs.bin",
            "iee_keyblob.bin",
        ),
        (
            "aes_xts512_rt1180",
            "iee_config.yaml",
            "encrypted_blob.bin",
            None,
        ),
    ],
)
def test_nxpimage_iee(cli_runner: CliRunner, tmpdir, data_dir, case, config, reference, keyblobs):
    work_dir = os.path.join(tmpdir, "iee", case)
    shutil.copytree(os.path.join(data_dir, "iee", case), work_dir)
    shutil.copy(os.path.join(data_dir, "iee", INPUT_BINARY), work_dir)

    with use_working_directory(work_dir):
        config_dict = load_configuration(config)
        out_dir = os.path.join(work_dir, config_dict["output_folder"])
        output_name = config_dict["output_name"]
        keyblob_name = config_dict["keyblob_name"]
        encrypted_name = config_dict["encrypted_name"]
        cmd = f"iee export -c {config}"
        cli_runner.invoke(nxpimage.main, cmd.split())
        assert os.path.isfile(os.path.join(out_dir, output_name))
        assert os.path.isfile(os.path.join(out_dir, encrypted_name))

        if reference:
            encrypted_image_enc = load_binary(reference)
            encrypted_nxpimage = load_binary(os.path.join(out_dir, encrypted_name))
            assert encrypted_image_enc == encrypted_nxpimage

        if keyblobs:
            assert os.path.isfile(os.path.join(out_dir, keyblob_name))
            reference_keyblob = load_binary(keyblobs)
            keyblobs_nxpimage = load_binary(os.path.join(out_dir, keyblob_name))
            assert reference_keyblob == keyblobs_nxpimage


@pytest.mark.parametrize(
    "family",
    [
        ("rt116x"),
        ("rt117x"),
        ("rt118x"),
    ],
)
def test_nxpimage_iee_template_cli(cli_runner: CliRunner, tmpdir, family):
    template = os.path.join(tmpdir, "iee_template.yaml")
    cmd = f"iee get-template --family {family} --output {template}"
    cli_runner.invoke(nxpimage.main, cmd.split())
    assert os.path.isfile(template)


@pytest.mark.parametrize(
    "case, config",
    [
        (
            "aes_xts512_custom_names",
            "iee_config.yaml",
        )
    ],
)
def test_iee_custom_output(cli_runner: CliRunner, tmpdir, data_dir, case, config):
    work_dir = os.path.join(tmpdir, "iee", case)
    shutil.copytree(os.path.join(data_dir, "iee", case), work_dir)
    shutil.copy(os.path.join(data_dir, "iee", INPUT_BINARY), work_dir)

    with use_working_directory(work_dir):
        config_dict = load_configuration(config)
        out_dir = os.path.join(work_dir, config_dict["output_folder"])
        config_dict["output_name"] = os.path.join(tmpdir, "iee_output")
        config_dict["keyblob_name"] = "keyblob"
        config_dict["encrypted_name"] = ""

        modified_config = os.path.join(work_dir, "modified_config.yaml")
        with open(modified_config, "w") as f:
            yaml.dump(config_dict, f)
        cmd = f"iee export -c {modified_config}"
        cli_runner.invoke(nxpimage.main, cmd.split())

        assert os.path.isfile(os.path.join(out_dir, "keyblob.bin"))
        assert not os.path.isfile(os.path.join(out_dir, "iee_rt117x_blhost.bcf"))
        assert os.path.isfile(os.path.join(out_dir, "readme.txt"))
        assert os.path.isfile(config_dict["output_name"] + ".bin")
