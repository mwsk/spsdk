#!/usr/bin/env python
# -*- coding: UTF-8 -*-
#
# Copyright 2023-2024 NXP
#
# SPDX-License-Identifier: BSD-3-Clause

"""API for communicating with the EL2GO server."""

import base64
import json
import logging
import math
import struct
import time
from enum import Enum
from typing import Optional, Union

from spsdk.el2go.client import EL2GOApiResponse, EL2GOClient, SPSDKHTTPClientError
from spsdk.el2go.secure_objects import SecureObjects
from spsdk.exceptions import SPSDKError, SPSDKUnsupportedOperation
from spsdk.mboot.mcuboot import McuBoot
from spsdk.utils.database import DatabaseManager, get_db, get_families, get_schema_file
from spsdk.utils.exceptions import SPSDKRegsErrorRegisterNotFound
from spsdk.utils.misc import Timeout, load_binary, value_to_int
from spsdk.utils.registers import Registers, RegsRegister

logger = logging.getLogger(__name__)


class GenStatus(list, Enum):  # type: ignore
    """Provisioning generation status."""

    GENERATION_TRIGGERED = ["GENERATION_TRIGGERED", "Creation of Secure Objects was started"]
    GENERATION_COMPLETED = ["GENERATION_COMPLETED", "Creation of Secure Objects is completed"]
    GENERATION_FAILED = ["GENERATION_FAILED", "Creation of Secure Objects failed"]
    PROVISIONING_COMPLETED = [
        "PROVISIONING_COMPLETED",
        "Secure Objects were provisioned successfully",
    ]
    PROVISIONING_FAILED = ["PROVISIONING_FAILED", "Provisioning of Secure Objects failed"]
    GENERATION_ON_CONNECTION = [
        "GENERATION_ON_CONNECTION",
        "Secure Object generation is triggered when a device request it",
    ]


OK_STATES = [
    GenStatus.GENERATION_COMPLETED.value[0],
    GenStatus.GENERATION_ON_CONNECTION.value[0],
    GenStatus.PROVISIONING_COMPLETED.value[0],
]
BAD_STATES = [GenStatus.GENERATION_FAILED.value[0], GenStatus.PROVISIONING_FAILED.value[0]]
WAIT_STATES = [GenStatus.GENERATION_TRIGGERED.value[0]]
NO_DATA_STATES = [
    GenStatus.GENERATION_ON_CONNECTION.value[0],
    GenStatus.PROVISIONING_COMPLETED.value[0],
]


class ProvisioningMethod(str, Enum):
    """Various types of TP methods."""

    DISPATCH_FW = "dispatch_fw"
    FW_USER_CONFIG = "fw_user_config"
    FW_DATA_SPLIT = "fw_data_split"


class CleanMethod(str, Enum):
    """Various types of Cleanup methods."""

    ERASE_CMPA = "erase_cmpa"
    NONE = "none"


class EL2GODomain(str, Enum):
    """EL2GO Domain types."""

    MATTER = "MATTER"
    RTP = "RTP"


EL2GO_DOMAINS = [EL2GODomain.MATTER.value, EL2GODomain.RTP.value]


class EL2GOTPClient(EL2GOClient):
    """EL2GO HTTP Client for TP operations."""

    DEFAULT_URL = "https://api.edgelock2go.com"

    def __init__(
        self,
        api_key: str,
        nc12: int,
        device_group_id: int,
        family: str,
        revision: str = "latest",
        url: str = "https://api.edgelock2go.com",
        timeout: int = 60,
        download_timeout: int = 300,
        delay: int = 5,
        **kwargs: str,
    ) -> None:
        """Initialize EL2GO TP HTTP Client.

        :param api_key: User EL2GO API key
        :param nc12: Product (12NC) number
        :param device_group_id: Device group to work with
        :param family: Target chip family
        :param revision: Target chip silicon revision, defaults to "latest"
        :param url: EL2GO Server API URL, defaults to "https://api.edgelock2go.com"
        :param timeout: Timeout for each API call, defaults to 60
        :param download_timeout: Timeout for downloading Secure Objects, defaults to 300
        :param delay: Delay between API calls when downloading Secure Objects, defaults to 5
        """
        self.nc12 = nc12
        self.device_group_id = device_group_id
        self.delay = delay
        self.download_timeout = download_timeout
        self.family = family
        self.revision = revision

        self.db = get_db(device=family, revision=revision)
        self.prov_method = ProvisioningMethod(
            self.db.get_str(DatabaseManager.EL2GO_TP, "prov_method")
        )
        self.hardware_family_type = self.db.get_str(DatabaseManager.EL2GO_TP, "el2go_name")
        self.fw_load_address = self.db.get_int(DatabaseManager.EL2GO_TP, "fw_load_address")

        self.prov_fw_path = kwargs.pop("prov_fw_path")
        self._prov_fw: Optional[bytes] = None
        self.tp_data_address = value_to_int(kwargs.pop("secure_objects_address"))
        self.clean_method = CleanMethod(self.db.get_str(DatabaseManager.EL2GO_TP, "clean_method"))
        self.domains = self._sanitize_domains(kwargs.pop("domains", EL2GO_DOMAINS))  # type: ignore

        super().__init__(api_key=api_key, url=url, timeout=timeout, raise_exceptions=True, **kwargs)

    @property
    def prov_fw(self) -> bytes:
        """Provisioning firmware binary."""
        if self._prov_fw is None:
            if not self.prov_fw_path:
                raise SPSDKError("Provisioning firmware path is not set")
            self._prov_fw = load_binary(self.prov_fw_path)
        return self._prov_fw

    def response_handling(self, response: EL2GOApiResponse, url: str) -> None:
        """Handle an error response.

        :raises SPSDKHTTPClientError: In case of an erroneous response (response code >= 400)
        """
        if response.status_code >= 400:
            raise SPSDKHTTPClientError(
                status_code=response.status_code,
                response=response.json_body,
                desc=(
                    f"Operation failed with status code {response.status_code} when calling {url}\n"
                    f"Response body: {json.dumps(response.json_body, indent=2)}\n"
                    f"Correlation id: {self.headers['EL2G-Correlation-ID']}"
                ),
            )

    def assign_device_to_devicegroup(
        self, device_id: str, allow_reassignment: bool = False
    ) -> None:
        """Assign a device to the configured group."""
        response, url = self._assign_device_to_group(device_id=device_id)
        if response.status_code == 422:
            # 'code' is not present in all responses
            if response.json_body["code"] == "ERR_SEDM_422_1":  # cspell:ignore SEDM
                logger.warning(response.json_body["details"])
                try:
                    # first check if the existing registration is the current group
                    self._find_device_group_id(device_id=device_id, group_id=self.device_group_id)
                    logger.warning("Device is already registered in the desired Device Group")
                except SPSDKError:
                    if not allow_reassignment:
                        raise
                    logger.warning(
                        "Device is already registered in a different Device Group. Attempting re-assignment."
                    )
                    # searching for group containing the device
                    # failure to find the device will throw an exception, thus no checking is done here
                    device_group = self._find_device_group_id(device_id=device_id)
                    logger.info(f"Device {device_id} found in group {device_group}")

                    response, url = self._unassign_device_from_group(
                        device_id=device_id, group_id=device_group
                    )
                    self.response_handling(response, url)
                    response, url = self._assign_device_to_group(device_id=device_id)
                    self.response_handling(response, url)
                return
        self.response_handling(response, url)

    def get_generation_status(self, device_id: str) -> str:
        """Get status of Secure Objects creation for given device."""
        url = f"/api/v1/rtp/devices/{device_id}/secure-object-provisionings"
        data = {
            "hardware-family-type": [self.hardware_family_type],
            "owner-domain-types": self.domains,
        }
        response = self._handle_el2go_request(method=self.Method.GET, url=url, param_data=data)
        self.response_handling(response=response, url=url)
        if not response.json_body["content"]:
            raise SPSDKError(
                f"No Secure Objects found for device {device_id} using domain(s): {', '.join(self.domains)}"
            )
        for provisioning in response.json_body["content"]:
            # fail early if generation failed
            if provisioning["provisioningState"] in BAD_STATES:
                raise SPSDKError(
                    f"Generating of Secure Objects failed. Correlation ID: {self.correlation_id}"
                )
            # return early with WAIT_STATE... no need to iterate further
            if provisioning["provisioningState"] in WAIT_STATES:
                return provisioning["provisioningState"]
        # everything else is a OK state
        return OK_STATES[0]

    def download_provisionings(self, device_id: str) -> dict:
        """Download provisionings for given device."""
        self._wait_for_provisionings(device_id=device_id)
        return self._download_provisionings(device_id=device_id)

    def _make_device_id_list(self, device_id: Union[str, list[str]]) -> list[str]:
        return [device_id] if isinstance(device_id, str) else device_id

    def _sanitize_domains(self, domains: list[str]) -> list[str]:
        """Sanitize domain types."""
        if not isinstance(domains, list):
            raise SPSDKError("Domains must be a list of strings")
        sanitized = [domain.upper() for domain in domains]
        for domain in sanitized:
            if domain not in EL2GO_DOMAINS:
                raise SPSDKError(f"Invalid domain type: {domain}")
        return sanitized

    def _download_provisionings(self, device_id: Union[str, list[str]]) -> dict:
        url = f"/api/v1/rtp/device-groups/{self.device_group_id}/devices/download-provisionings"
        data = {
            "productHardwareFamilyType": self.hardware_family_type,
            "deviceIds": self._make_device_id_list(device_id),
        }
        param_data = {"owner-domain-types": self.domains}
        response = self._handle_el2go_request(
            method=self.Method.POST, url=url, json_data=data, param_data=param_data
        )
        self.response_handling(response=response, url=url)
        return response.json_body

    def _wait_for_provisionings(self, device_id: str) -> None:
        time.sleep(2)
        timeout = Timeout(self.download_timeout)
        while not timeout.overflow():
            status = self.get_generation_status(device_id=device_id)
            if status in BAD_STATES:
                # we should never get here, but just in case
                raise SPSDKError(
                    f"Generating of secure objects failed. EL2G-Correlation-ID: {self.correlation_id}"
                )
            if status in WAIT_STATES:
                time.sleep(self.delay)
                continue
            if status in OK_STATES:
                return
        raise SPSDKError(
            f"Waiting for Secure Objects generation timed-out. EL2G-Correlation-ID: {self.correlation_id}"
        )

    def download_secure_objects(self, device_id: str) -> bytes:
        """Download all secure objects for given device."""
        provisionings = self.download_provisionings(device_id=device_id)
        data = self.serialize_provisionings(provisionings=provisionings)
        return data

    def serialize_provisionings(self, provisionings: dict) -> bytes:
        """Serialize Secure Objects from JSON object to bytes."""
        data = bytes()
        for dev_prov in provisionings:
            for rtp_prov in dev_prov["rtpProvisionings"]:
                if rtp_prov["state"] == GenStatus.GENERATION_COMPLETED.value[0]:
                    data += base64.b64decode(rtp_prov["apdus"]["createApdu"]["apdu"])
                if rtp_prov["state"] == GenStatus.GENERATION_ON_CONNECTION.value[0]:
                    logger.warning(
                        f"Provisioning ID: {rtp_prov['provisioningId']} will be created upon connection"
                    )
                if rtp_prov["state"] == GenStatus.PROVISIONING_COMPLETED.value[0]:
                    logger.warning(
                        f"Provisioning ID {rtp_prov['provisioningId']} already provisioned ({rtp_prov['state']})"
                    )
        return data

    def _serialize_single_provisioning(self, provisioning: dict) -> bytes:
        data = bytes()
        for rtp_prov in provisioning["rtpProvisionings"]:
            if rtp_prov["state"] == GenStatus.GENERATION_COMPLETED.value[0]:
                data += base64.b64decode(rtp_prov["apdus"]["createApdu"]["apdu"])
            if rtp_prov["state"] == GenStatus.GENERATION_ON_CONNECTION.value[0]:
                logger.warning(
                    f"Provisioning ID: {rtp_prov['provisioningId']} will be created upon connection"
                )
            if rtp_prov["state"] == GenStatus.PROVISIONING_COMPLETED.value[0]:
                logger.warning(
                    f"Provisioning ID {rtp_prov['provisioningId']} already provisioned ({rtp_prov['state']})"
                )
        return data

    def _find_device_group_id(
        self, device_id: str, group_id: Optional[Union[str, int]] = None
    ) -> str:
        """Find GROUP_ID in which the given device is assigned."""
        if group_id:
            candidates = [group_id]
        else:
            url = f"/api/v1/products/{self.nc12}/device-groups"
            response = self._handle_el2go_request(method=self.Method.GET, url=url)
            self.response_handling(response=response, url=url)
            candidates = [group["id"] for group in response.json_body["content"]]

        for check_id in candidates:
            url = f"/api/v1/products/{self.nc12}/device-groups/{check_id}/devices/{device_id}"
            response = self._handle_el2go_request(method=self.Method.GET, url=url)
            if response.status_code == 200:
                return str(check_id)

            if response.status_code == 404:
                continue
            else:
                self.response_handling(response=response, url=url)

        raise SPSDKError(
            f"Device {device_id} was not found in product {self.nc12} for group(s): {candidates}"
        )

    def _assign_device_to_group(
        self, device_id: Union[str, list[str]]
    ) -> tuple[EL2GOApiResponse, str]:
        """Assign a device to the configured group."""
        url = f"/api/v1/products/{self.nc12}/device-groups/{self.device_group_id}/devices"
        data = {
            "deviceIds": self._make_device_id_list(device_id),
        }
        response = self._handle_el2go_request(method=self.Method.POST, url=url, json_data=data)
        return response, url

    def _unassign_device_from_group(
        self, device_id: Union[str, list[str]], group_id: Optional[str] = None
    ) -> tuple[EL2GOApiResponse, str]:
        """Unassign a device from the device group."""
        url = f"/api/v1/products/{self.nc12}/device-groups/{group_id or self.device_group_id}/devices/unclaim"
        data = {
            "deviceIds": self._make_device_id_list(device_id),
        }
        response = self._handle_el2go_request(method=self.Method.POST, url=url, json_data=data)
        # attempt to minimize the impact of el2go race conditions
        wait_time = 10
        logger.info(f"Waiting for {wait_time} seconds to allow EL2GO to process the request")
        time.sleep(wait_time)
        return response, url

    def get_test_connection_response(self) -> EL2GOApiResponse:
        """Test connection to EL2GO."""
        url = f"/api/v1/products/{self.nc12}/device-groups/{self.device_group_id}"
        return self._handle_el2go_request(method=self.Method.GET, url=url)

    def test_connection(self) -> None:
        """Test connection to EL2GO service.

        :raises SPSDKHTTPClientError: In case of an erroneous response (response code >= 400)
        """
        url = f"/api/v1/products/{self.nc12}/device-groups/{self.device_group_id}"
        response = self.get_test_connection_response()
        self.response_handling(response, url)

    def register_devices(self, uuids: list[str]) -> str:
        """Register job for UUIDs."""
        logger.info(f"Submitting job for {len(uuids)} devices")
        url = f"/api/v1/products/{self.nc12}/device-groups/{self.device_group_id}/register-devices"
        data = {"deviceIds": uuids}
        response = self._handle_el2go_request(self.Method.POST, url=url, json_data=data)
        self.response_handling(response, url)
        return response.json_body["jobId"]

    def get_job_details(self, job_id: str) -> Optional[dict]:
        """Get job details."""
        logger.info(f"Getting job details for {job_id}")
        url = f"/api/v1/rtp/jobs/{job_id}"
        response = self._handle_el2go_request(self.Method.GET, url=url)
        if response.status_code == self.Status.NOT_FOUND:
            return None
        return response.json_body

    @classmethod
    def calculate_jobs(cls, uuid_count: int, max_job_size: int = 500) -> list[int]:
        """Calculate number of jobs and their sizes for given number if devices."""
        jobs = math.ceil(uuid_count / max_job_size)
        size = math.ceil(uuid_count / jobs)
        result = [size] * jobs
        extra = size * jobs - uuid_count
        for i in range(extra):
            result[i] -= 1
        return result

    @classmethod
    def split_uuids_to_jobs(cls, uuids: list[str], max_job_size: int) -> list[list[str]]:
        """Split UUIDs into jobs with given max size."""
        job_sizes = cls.calculate_jobs(len(uuids), max_job_size)
        iterator = iter(uuids)
        jobs: list[list[str]] = []
        for job_size in job_sizes:
            group_uuids = [next(iterator) for _ in range(job_size)]
            jobs.append(group_uuids)
        return jobs

    def create_user_config(self) -> tuple[bytes, int, int]:
        """Create EL2GO User Config blob.

        :return: User config blob, address for user config, address for user data.
        """
        if self.prov_method == ProvisioningMethod.FW_USER_CONFIG:
            fw_read_address = self.db.get_int(DatabaseManager.EL2GO_TP, "fw_read_address")
            user_data_address = self.db.get_int(DatabaseManager.EL2GO_TP, "user_data_address")
            tp_data_address = self.tp_data_address
            user_config = (
                b"ELUC"  # cspell:ignore ELUC
                + user_data_address.to_bytes(length=4, byteorder="little")
                + tp_data_address.to_bytes(length=4, byteorder="little")
                + bytes(20)
            )

            return user_config, fw_read_address, user_data_address

        if self.prov_method == ProvisioningMethod.FW_DATA_SPLIT:
            fw_read_address = self.db.get_int(DatabaseManager.EL2GO_TP, "fw_read_address")
            tp_data_address = self.tp_data_address
            return b"", fw_read_address, tp_data_address

        if self.prov_method == ProvisioningMethod.DISPATCH_FW:
            return b"", 0, self.tp_data_address

        raise SPSDKUnsupportedOperation(
            f"Provisioning method '{self.prov_method}' is not supported by '{self.family}'"
        )

    @property
    def use_data_split(self) -> bool:
        """Device family uses TP FW with data split into user and standard blocks."""
        return self.prov_method == ProvisioningMethod.FW_DATA_SPLIT

    @property
    def use_user_config(self) -> bool:
        """Device family uses TP FW with user config block."""
        return self.prov_method == ProvisioningMethod.FW_USER_CONFIG

    @property
    def use_dispatch_fw(self) -> bool:
        """Device family uses TP FW with dispatch (trigger via blhost)."""
        return self.prov_method == ProvisioningMethod.DISPATCH_FW

    def run_cleanup_method(self, mboot: McuBoot) -> None:
        """Run cleanup method."""
        if self.clean_method == CleanMethod.NONE:
            logger.info(f"Device {self.family} doesn't have a registered cleanup method")
            return
        if self.clean_method == CleanMethod.ERASE_CMPA:
            # don't use top-level import to save time in production, where this functionality is not required
            from spsdk.pfr.pfr import CMPA

            cmpa = CMPA(family=self.family, revision=self.revision)
            cmpa.set_config({})
            cmpa_data = cmpa.export(draw=False)
            cmpa_address = self.db.get_int(DatabaseManager.PFR, ["cmpa", "address"])

            mboot.write_memory(address=cmpa_address, data=cmpa_data)
            return

        raise SPSDKUnsupportedOperation(f"Unsupported cleanup method {self.clean_method}")

    @classmethod
    # type: ignore[override]
    # pylint: disable=arguments-differ
    def get_validation_schema(cls, family: str, revision: str = "latest") -> dict:
        """Get JSON schema for validating configuration data."""
        schema_file = get_schema_file(DatabaseManager.EL2GO_TP)
        schema = schema_file["el2go_tp"]
        db = get_db(device=family, revision=revision)

        # fw_load_address = db.get_int(DatabaseManager.EL2GO_TP, "fw_load_address")
        # if fw_load_address > 0:
        #     schema["properties"].update(schema_file["fw_load_address"]["properties"])
        #     schema["properties"]["fw_load_address"]["template_value"] = fw_load_address
        #     schema["required"].extend(schema_file["fw_load_address"]["required"])

        use_additional_data = db.get_bool(DatabaseManager.EL2GO_TP, "use_additional_data")
        if use_additional_data:
            schema["properties"].update(schema_file["additional_data_address"]["properties"])
            schema["required"].extend(schema_file["additional_data_address"]["required"])
        else:
            schema["properties"].update(schema_file["secure_objects_address"]["properties"])
            schema["required"].extend(schema_file["secure_objects_address"]["required"])

        return schema

    @classmethod
    def get_supported_families(cls) -> list[str]:
        """Get family names supported by WPCTarget."""
        return get_families(DatabaseManager.EL2GO_TP)

    @classmethod
    # type: ignore[override]
    # pylint: disable=arguments-differ
    def generate_config_template(cls, family: str, revision: str = "latest") -> str:
        """Generate configuration YAML template for given family."""
        schema = cls.get_validation_schema(family=family, revision=revision)
        schema["properties"]["family"]["template_value"] = family
        schema["properties"]["revision"]["template_value"] = revision
        return super().generate_config_template(
            schemas=[schema],
            title=f"Configuration of EdgeLock 2GO Offline Provisioning flow for {family}",
        )


def split_user_data(data: bytes) -> tuple[bytes, bytes]:
    """Split TLV binary into Internal and External blocks."""
    internal, external = SecureObjects.parse(data).split_int_ext()
    return internal, external


def get_el2go_otp_binary(config_data: Union[list, dict], family: Optional[str] = None) -> bytes:
    """Create EL2GO OTP Binary from the user config data."""
    if isinstance(config_data, list):
        if not family:
            raise SPSDKError("When using SEC Tool's JSON config, you need to specify family.")
    elif isinstance(config_data, dict):
        # schema validation goes here
        family = config_data["family"]

    user_regs: list[RegsRegister] = []
    assert isinstance(family, str)
    defaults = Registers(family=family, feature=DatabaseManager.FUSES)

    if isinstance(config_data, list):
        # SEC Tool's config file
        page_info: dict = config_data.pop(0)
        page_type = page_info.get("page_type")
        if page_type != "OTP":
            raise SPSDKError("Invalid config file format. Missing OTP page_type.")
        user_data: dict = config_data.pop(0)
        for user_reg_name, user_reg_value in user_data.items():
            try:
                reg = defaults.find_reg(name=user_reg_name)
            except SPSDKRegsErrorRegisterNotFound:
                logger.warning(f"Register {user_reg_name} was not found")
                continue
            if isinstance(user_reg_value, (str, int)):
                reg.set_value(user_reg_value)
            if isinstance(user_reg_value, dict):
                for bf_name, bf_value in user_reg_value.items():
                    bf = reg.find_bitfield(bf_name)
                    bf.set_value(bf_value)
            user_regs.append(reg)

    else:
        raise SPSDKUnsupportedOperation(
            "Support for dedicated SPSDK config file is not yet implemented"
        )

    if not user_regs:
        raise SPSDKError("No OTP fuses were decoded from the user configuration.")

    data = bytes()
    ignored = _get_ignored_otp_indexes(family=family)
    for user_reg in user_regs:
        if user_reg.otp_index in ignored:
            logger.info(f"Ignoring OTP: {user_reg.uid} ({user_reg.name})")
            continue

        # EL2GO OTP Binary TLV format for single OTP
        # Tag: 0x40 (1B); Length 2 (1B), Value: OTP index (2B)
        # Tag: 0x41 (1B); Length 2/4 (1B), Value: OTP value (2/4B)
        logger.info(
            f"Adding OTP: {user_reg.uid} ({user_reg.name}), value: {hex(user_reg.get_value())}"
        )
        # cspell:ignore BBHBB
        data += struct.pack(">BBHBB", 0x40, 2, user_reg.otp_index, 0x41, user_reg.width // 8)
        data += int.to_bytes(user_reg.get_value(), length=user_reg.width // 8, byteorder="big")

    return data


def _get_ignored_otp_indexes(family: str) -> list[int]:
    """Get all list of indexes of OTPs that should be ignored."""
    result: list[int] = []
    db = get_db(device=family)
    ignored_otp = db.get_list(DatabaseManager.EL2GO_TP, "ignored_otp", default=[])
    ignored_otp_ranges = db.get_list(DatabaseManager.EL2GO_TP, "ignored_otp_ranges", default=[])
    for idx in ignored_otp:
        result.append(idx)
    # get pairwise elements from list (0, 1), (2, 3)...
    for start, end in zip(ignored_otp_ranges[0::2], ignored_otp_ranges[1::2]):
        for idx in range(start, end + 1):
            result.append(idx)
    return result