#
# SPDX-License-Identifier: BSD-2-Clause
#
# Copyright (c) 2020 Alex Richardson
#
# This work was supported by Innovate UK project 105694, "Digital Security by
# Design (DSbD) Technology Platform Prototype".
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
import os
from pathlib import Path

from .disk_image import BuildCheriBSDDiskImage
from .project import SimpleProject
from ..config.compilation_targets import CompilationTargets
from ..config.loader import ComputedDefaultValue
from ..utils import OSInfo, set_env


class InstallMorelloFVP(SimpleProject):
    target = "install-morello-fvp"
    container_name = "morello-fvp"

    @classmethod
    def setup_config_options(cls, **kwargs):
        super().setup_config_options(**kwargs)
        cls.installer_path = cls.add_path_option("installer-path", help="Path to the FVP installer.sh")
        # We can run the FVP on macOS by using docker. FreeBSD might be able to use Linux emulation.
        cls.use_docker_container = cls.add_path_option("use-docker-container", default=OSInfo.IS_MAC,
                                                       help="Run the FVP inside a docker container")

    @property
    def install_dir(self):
        return self.config.morello_sdk_dir / "FVP_Morello"

    def process(self):
        if not self.installer_path:
            self.fatal("Path to installer not known, set the", "--" + self.get_config_option_name("installer_path"),
                       "config option!")
            return
        self.makedirs(self.install_dir)
        if self.use_docker_container:
            self.install_file(self.installer_path, self.install_dir / self.installer_path.name)
            self.write_file(self.install_dir / "Dockerfile", contents="""
FROM opensuse/leap:15.2
COPY {installer_name} .
RUN zypper in -y xterm gzip tar libdbus-1-3 libatomic1 telnet
RUN ./{installer_name} --i-agree-to-the-contained-eula --no-interactive --destination=/opt/FVP_Morello && \
    rm ./{installer_name}
# Run as non-root user to allow X11 to work
RUN useradd fvp-user
USER fvp-user
VOLUME /diskimg
""".format(installer_name=self.installer_path.name), overwrite=True)
            self.run_cmd("docker", "build", "--pull", "-t", self.container_name, ".", cwd=self.install_dir)
        else:
            self.run_cmd(self.installer_path, "--i-agree-to-the-contained-eula", "--no-interactive",
                         "-destination=" + str(self.install_dir))

    def _plugin_args(self):
        plugin_path = "plugins/Linux64_GCC-6.4/MorelloPlugin.so"
        if self.use_docker_container:
            return ["--plugin", Path("/opt/FVP_Morello", plugin_path)]
        return ["--plugin", self.ensure_file_exists("Morello FVP plugin", self.install_dir / plugin_path)]

    def execute_fvp(self, args: list, disk_image_path: Path = None, firmware_path: Path = None, **kwargs):
        model_relpath = "models/Linux64_GCC-6.4/FVP_Morello"
        if self.use_docker_container:
            base_cmd = ["docker", "run", "-it", "--rm",
                        "-p", "5000:5000",  # Expose port 5000 for telnet
                        ]
            if disk_image_path is not None:
                base_cmd += ["-v", str(disk_image_path) + ":" + str(disk_image_path)]
            if firmware_path is not None:
                base_cmd += ["-v", str(firmware_path) + ":" + str(firmware_path)]
            base_cmd += [self.container_name, Path("/opt/FVP_Morello", model_relpath)]
        else:
            base_cmd = [self.install_dir / model_relpath]
        # TODO: X11 on macos:
        "socat TCP-LISTEN:6000,reuseaddr,fork UNIX-CLIENT:\"$DISPLAY\""
        self.run_cmd(base_cmd + self._plugin_args() + args, **kwargs)

    def run_tests(self):
        self.execute_fvp(["--help"])
        self.execute_fvp(["--cyclelimit", "1000"])


class LaunchFVPBase(SimpleProject):
    do_not_add_to_targets = True
    _source_class = BuildCheriBSDDiskImage
    dependencies = [_source_class.target]
    supported_architectures = _source_class.supported_architectures

    def setup(self):
        super().setup()
        assert self.crosscompile_target.is_aarch64(include_purecap=True)

    @staticmethod
    def default_ssh_port():
        # chose a different port for each user (hopefully it isn't in use yet)
        return 12345 + ((os.getuid() - 1000) % 10000)

    @classmethod
    def setup_config_options(cls, **kwargs):
        super().setup_config_options(**kwargs)

        fw_default = ComputedDefaultValue(function=lambda c, _: c.morello_sdk_dir / "fvp-firmware",
                                          as_string="<MORELLO_SDK>/fvp-firmware")
        cls.firmware_path = cls.add_path_option("firmware-path", default=fw_default,
                                                help="Path to the UEFI firmware binaries")
        cls.remote_disk_image_path = cls.add_config_option("remote-disk-image-path",
                                                           help="When set rsync will be used to update the image from "
                                                                "the remote server prior to running it.")
        cls.ssh_port = cls.add_config_option("ssh-port", default=cls.default_ssh_port(), kind=int)
        # Allow using the architectural FVP:
        cls.use_architectureal_fvp = cls.add_bool_option("use-architectural-fvp",
                                                         help="Use the architectural FVP that requires a license.")
        cls.license_server = cls.add_config_option("license-server", help="License server to use for the model")
        cls.arch_model_path = cls.add_path_option("simulator-path", help="Path to the FVP Model",
                                                  default="/usr/local/FVP_Base_RevC-Rainier")

    # noinspection PyAttributeOutsideInit
    def process(self):
        if not self.firmware_path.exists():
            self.fatal("Firmware path", self.firmware_path, " is invalid, set the",
                       "--" + self.get_config_option_name("firmware_path"), "config option!")
        bl1_bin = self.ensure_file_exists("bl1.bin firmware", self.firmware_path / "bl1.bin")
        fip_bin = self.ensure_file_exists("fip.bin firmware", self.firmware_path / "fip.bin")
        if self.remote_disk_image_path:
            self.copy_remote_file(self.remote_disk_image_path,
                                  self._source_class.get_instance(self).disk_image_path)
        disk_image = self.ensure_file_exists("disk image", self._source_class.get_instance(self).disk_image_path)
        # TODO: tracing support:
        # TARMAC_TRACE="--plugin ${PLUGIN_DIR}/TarmacTrace.so"
        # TARMAC_TRACE="${TARMAC_TRACE} -C TRACE.TarmacTrace.trace-file=${HOME}/rainier/rainier.tarmac.trace"
        # TARMAC_TRACE="${TARMAC_TRACE} -C TRACE.TarmacTrace.quantum-size=0x1"
        # TARMAC_TRACE="${TARMAC_TRACE} -C TRACE.TarmacTrace.start-instruction-count=4400000000" # just after login
        # ARCH_MSG="--plugin ${PLUGIN_DIR}/ArchMsgTrace.so -C
        # ARCH_MSG="${ARCH_MSG} -C TRACE.ArchMsgTrace.trace-file=${HOME}/rainier/rainier.archmsg.trace"
        model_params = []

        def add_board_params(*params):
            prefix = "bp." if self.use_architectureal_fvp else "board."
            model_params.extend([prefix + p for p in params])

        add_board_params(
            "smsc_91c111.enabled=1",
            "hostbridge.userNetworking=true",
            "hostbridge.userNetPorts=" + str(self.ssh_port) + "=22",
            "hostbridge.interfaceName=ARM0",
            "virtio_net.enabled=0",
            "virtio_net.transport=legacy",
            "virtio_net.hostbridge.userNetworking=1",
            "virtioblockdevice.image_path=" + str(disk_image))

        if self.use_architectureal_fvp:
            if not self.license_server:
                self.license_server = "unknown.license.server"  # for --pretend
                self.fatal("License server info unknown, set the",
                           "--" + self.get_config_option_name("license_server"),
                           "config option!")
            if not self.arch_model_path.is_dir():
                self.fatal("FVP path", self.arch_model_path, "does not exist, set the",
                           "--" + self.get_config_option_name("simulator_path"), "config option!")

            with set_env(ARMLMD_LICENSE_FILE=self.license_server, print_verbose_only=False):
                sim_binary = self.ensure_file_exists("Model binary",
                                                     self.arch_model_path /
                                                     "models/Linux64_GCC-6.4/FVP_Base_RevC-Rainier")
                plugin = self.ensure_file_exists("Morello FVP plugin",
                                                 self.arch_model_path / "plugins/Linux64_GCC-6.4/MorelloPlugin.so")
                # prepend -C to each of the parameters:
                param_cmd_args = [x for param in model_params for x in ("-C", param)]
                self.run_cmd([sim_binary, "--plugin", plugin, "--print-port-number"] + param_cmd_args)
            model_params += [
                "pctl.startup=0.0.0.0",
                "bp.secure_memory=0",
                "cache_state_modelled=0",
                "cluster0.NUM_CORES=1",
                "flashloader0.fname=" + str(fip_bin),
                "secureflashloader.fname=" + str(bl1_bin),
                ]
        else:
            # prepend -C to each of the parameters:
            model_params += [
                # "css.cache_state_modelled=0",
                # XXX: or is it , css.scp.ROMloader.fname css.mcp.ROMloader.fname?
                "css.nonTrustedROMloader.fname=" + str(fip_bin),
                "css.trustedBootROMloader.fname=" + str(bl1_bin),
                "css.mcp.ROMloader.fname=" + str(
                    self.ensure_file_exists("MCP ROM FW",
                                            self.firmware_path / "morello/mcp_romfw/release/bin/firmware.bin")),
                "soc.mcp_qspi_loader.fname=" + str(
                    self.ensure_file_exists("MCP RAM FW",
                                            self.firmware_path / "morello/mcp_ramfw_fvp/release/bin/firmware.bin")),
                "css.scp.ROMloader.fname=" + str(
                    self.ensure_file_exists("SCP ROM FW",
                                            self.firmware_path / "morello/scp_romfw/release/bin/firmware.bin")),
                "soc.scp_qspi_loader.fname=" + str(
                    self.ensure_file_exists("SCP RAM FW",
                                            self.firmware_path / "morello/scp_ramfw_fvp/release/bin/firmware.bin")),
                # "num_clusters=1",
                # "num_cores=1",
                ]
            param_cmd_args = [x for param in model_params for x in ("-C", param)]
            InstallMorelloFVP.get_instance(self, cross_target=CompilationTargets.NATIVE).execute_fvp(
                param_cmd_args + ["--print-port-number"], disk_image_path=disk_image, firmware_path=self.firmware_path)


class LaunchFVPCheriBSD(LaunchFVPBase):
    target = "run-fvp"
    _source_class = BuildCheriBSDDiskImage
    supported_architectures = [CompilationTargets.CHERIBSD_MORELLO_HYBRID, CompilationTargets.CHERIBSD_MORELLO_PURECAP]