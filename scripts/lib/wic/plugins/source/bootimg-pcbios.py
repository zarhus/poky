#
# Copyright (c) 2014, Intel Corporation.
#
# SPDX-License-Identifier: GPL-2.0-only
#
# DESCRIPTION
# This implements the 'bootimg-pcbios' source plugin class for 'wic'
#
# AUTHORS
# Tom Zanussi <tom.zanussi (at] linux.intel.com>
#

import logging
import os
import re
import shutil

from wic import WicError
from wic.engine import get_custom_config
from wic.pluginbase import SourcePlugin
from wic.misc import (exec_cmd, exec_native_cmd,
                      get_bitbake_var, BOOTDD_EXTRA_SPACE)

logger = logging.getLogger('wic')

class BootimgPcbiosPlugin(SourcePlugin):
    """
    Create MBR boot partition.
    This plugin supports syslinux and GRUB 2 bootloaders.
    """

    name = 'bootimg-pcbios'

    @classmethod
    def _create_grub_core_img(cls, grubdir):
        """
        Create the core image in the grub directory.
        """
        grub_modules = "at_keyboard biosdisk boot chain configfile ext2 fat linux ls part_msdos reboot serial vga"
        cmd_mkimage = "grub-mkimage -p %s -d %s -o %s/core.img -O i386-pc %s" % (
                       "(hd0,msdos1)/grub",
                       grubdir,
                       grubdir,
                       grub_modules)

        exec_cmd(cmd_mkimage)

    @classmethod
    def _get_bootimg_dir(cls, bootimg_dir, dirname):
        """
        Check if dirname exists in default bootimg_dir or in STAGING_DIR.
        """
        staging_datadir = get_bitbake_var("STAGING_DATADIR")
        for result in (bootimg_dir, staging_datadir):
            if os.path.exists("%s/%s" % (result, dirname)):
                return result

        # STAGING_DATADIR is expanded with MLPREFIX if multilib is enabled
        # but dependency syslinux is still populated to original STAGING_DATADIR
        nonarch_datadir = re.sub('/[^/]*recipe-sysroot', '/recipe-sysroot', staging_datadir)
        if os.path.exists(os.path.join(nonarch_datadir, dirname)):
            return nonarch_datadir

        raise WicError("Couldn't find correct bootimg_dir, exiting")

    @classmethod
    def do_install_disk(cls, disk, disk_name, creator, workdir, oe_builddir,
                        bootimg_dir, kernel_dir, native_sysroot):
        """
        Called after all partitions have been prepared and assembled into a
        disk image.  In this case, we install the MBR.
        """
        bootimg_dir = cls._get_bootimg_dir(bootimg_dir, 'syslinux')
        mbrfile = "%s/syslinux/" % bootimg_dir
        if creator.ptable_format == 'msdos':
            mbrfile += "mbr.bin"
        elif creator.ptable_format == 'gpt':
            mbrfile += "gptmbr.bin"
        else:
            raise WicError("Unsupported partition table: %s" %
                           creator.ptable_format)

        if not os.path.exists(mbrfile):
            raise WicError("Couldn't find %s.  If using the -e option, do you "
                           "have the right MACHINE set in local.conf?  If not, "
                           "is the bootimg_dir path correct?" % mbrfile)

        full_path = creator._full_path(workdir, disk_name, "direct")
        logger.debug("Installing MBR on disk %s as %s with size %s bytes",
                     disk_name, full_path, disk.min_size)

        dd_cmd = "dd if=%s of=%s conv=notrunc" % (mbrfile, full_path)
        exec_cmd(dd_cmd, native_sysroot)

        device_map_path = "%s/device.map" % workdir
        device_map_content = "(hd0) %s" % full_path
        with open(device_map_path, 'w') as file:
            file.write(device_map_content)

        # We need to call grub-bios-setup to actually install GRUB on disk. It needs
        # to be called on the disk file, as opposed to the syslinux, to be called on
        # the partition.
        # TODO: There is no way to access source_params from the do_install_disk?
        source_params = dict()
        source_params['loader-pcbios'] = 'grub'
        if source_params['loader-pcbios'] == 'grub':

            grub_dir = os.path.join(workdir, "hdd/boot/grub/i386-pc")
            cmd_bios_setup = 'grub-bios-setup -v --device-map=%s -r "hd0,msdos1" -d %s %s' % (
                              device_map_path,
                              grub_dir,
                              full_path
                              )
            exec_cmd(cmd_bios_setup, native_sysroot)

    @classmethod
    def do_configure_grub(cls, hdddir, creator, cr_workdir, source_params):
        """
        Creates loader-specific (grub) config
        """

        # Create config file
        bootloader = creator.ks.bootloader

        grubdir = os.path.join(hdddir, "grub")
        install_cmd = "install -d %s" % grubdir
        exec_cmd(install_cmd)

        deploy_dir = get_bitbake_var("DEPLOY_DIR_IMAGE")

        custom_cfg = None
        if bootloader.configfile:
            custom_cfg = get_custom_config(bootloader.configfile)
            if custom_cfg:
                # Use a custom configuration for grub
                grub_conf = custom_cfg
                logger.debug("Using custom configuration file %s "
                             "for grub.cfg", bootloader.configfile)
            else:
                raise WicError("configfile is specified but failed to "
                               "get it from %s." % bootloader.configfile)

        initrd = source_params.get('initrd')

        if not custom_cfg:
            # Create grub configuration using parameters from wks file
            bootloader = creator.ks.bootloader
            title = source_params.get('title')

            grub_conf = ""
            grub_conf += "serial --unit=0 --speed=115200 --word=8 --parity=no --stop=1\n"
            grub_conf += "default=boot\n"
            grub_conf += "timeout=%s\n" % bootloader.timeout
            grub_conf += "menuentry '%s'{\n" % (title if title else "boot")

            kernel = get_bitbake_var("KERNEL_IMAGETYPE")
            if get_bitbake_var("INITRAMFS_IMAGE_BUNDLE") == "1":
                if get_bitbake_var("INITRAMFS_IMAGE"):
                    kernel = "%s-%s.bin" % \
                        (get_bitbake_var("KERNEL_IMAGETYPE"), get_bitbake_var("INITRAMFS_LINK_NAME"))

            label = source_params.get('label')
            label_conf = "root=%s" % creator.rootdev
            if label:
                label_conf = "LABEL=%s" % label

            grub_conf += "linux /%s %s rootwait %s\n" \
                % (kernel, label_conf, bootloader.append)

            if initrd:
                initrds = initrd.split(';')
                grub_conf += "initrd"
                for rd in initrds:
                    grub_conf += " /%s" % rd
                grub_conf += "\n"

            grub_conf += "}\n"

        logger.debug("Writing grub config %s/grub.cfg", grubdir)
        cfg = open("%s/grub.cfg" % grubdir, "w")
        cfg.write(grub_conf)
        cfg.close()

    @classmethod
    def do_configure_syslinux(cls, hdddir, creator, cr_workdir, source_params):
        """
        Creates loader-specific (syslinux) config
        """

        bootloader = creator.ks.bootloader

        custom_cfg = None
        if bootloader.configfile:
            custom_cfg = get_custom_config(bootloader.configfile)
            if custom_cfg:
                # Use a custom configuration for syslinux
                syslinux_conf = custom_cfg
                logger.debug("Using custom configuration file %s "
                             "for syslinux.cfg", bootloader.configfile)
            else:
                raise WicError("configfile is specified but failed to "
                               "get it from %s." % bootloader.configfile)

        if not custom_cfg:
            # Create syslinux configuration using parameters from wks file
            splash = os.path.join(cr_workdir, "/hdd/boot/splash.jpg")
            if os.path.exists(splash):
                splashline = "menu background splash.jpg"
            else:
                splashline = ""

            syslinux_conf = ""
            syslinux_conf += "PROMPT 0\n"
            syslinux_conf += "TIMEOUT " + str(bootloader.timeout) + "\n"
            syslinux_conf += "\n"
            syslinux_conf += "ALLOWOPTIONS 1\n"
            syslinux_conf += "SERIAL 0 115200\n"
            syslinux_conf += "\n"
            if splashline:
                syslinux_conf += "%s\n" % splashline
            syslinux_conf += "DEFAULT boot\n"
            syslinux_conf += "LABEL boot\n"

            kernel = "/" + get_bitbake_var("KERNEL_IMAGETYPE")
            syslinux_conf += "KERNEL " + kernel + "\n"

            syslinux_conf += "APPEND label=boot root=%s %s\n" % \
                             (creator.rootdev, bootloader.append)

        logger.debug("Writing syslinux config %s/hdd/boot/syslinux.cfg",
                     cr_workdir)
        cfg = open("%s/hdd/boot/syslinux.cfg" % cr_workdir, "w")
        cfg.write(syslinux_conf)
        cfg.close()

    @classmethod
    def do_configure_partition(cls, part, source_params, creator, cr_workdir,
                               oe_builddir, bootimg_dir, kernel_dir,
                               native_sysroot):
        """
        Called before do_prepare_partition(), creates loader-specific config
        """
        hdddir = "%s/hdd/boot" % cr_workdir

        install_cmd = "install -d %s" % hdddir
        exec_cmd(install_cmd)

        try:
            if source_params['loader-pcbios'] == 'grub':
                cls.do_configure_grub(hdddir, creator, cr_workdir, source_params)
            elif source_params['loader-pcbios'] == 'syslinux':
                cls.do_configure_syslinux(hdddir, creator, cr_workdir, source_params)
            else:
                raise WicError("unrecognized bootimg-pcbios loader: %s" % source_params['loader-pcbios'])
        except KeyError:
            raise WicError("bootimg-pcbios requires a loader, none specified")

    @classmethod
    def do_prepare_grub(cls, part, hdddir, bootimg_dir, staging_kernel_dir):
        """
        Partition preparation specific to grub loader
        """

        # Copying grub modules
        grub_dir = os.path.join(hdddir, "grub/i386-pc")
        grub_dir_native = os.path.join(get_bitbake_var("IMAGE_ROOTFS"), "usr/lib/grub/i386-pc")
        logger.debug("Copying grub modules from: %s to: %s", grub_dir_native, grub_dir)
        shutil.copytree(grub_dir_native, grub_dir)

        cls._create_grub_core_img(grub_dir)


    @classmethod
    def do_prepare_syslinux(cls, part, hdddir, bootimg_dir, staging_kernel_dir):
        """
        Partition preparation specific to syslinux loader
        """

        cmds = ("install -m 444 %s/syslinux/ldlinux.sys %s/ldlinux.sys" %
                (bootimg_dir, hdddir),
                "install -m 0644 %s/syslinux/vesamenu.c32 %s/vesamenu.c32" %
                (bootimg_dir, hdddir),
                "install -m 444 %s/syslinux/libcom32.c32 %s/libcom32.c32" %
                (bootimg_dir, hdddir),
                "install -m 444 %s/syslinux/libutil.c32 %s/libutil.c32" %
                (bootimg_dir, hdddir))

        for install_cmd in cmds:
            exec_cmd(install_cmd)

    @classmethod
    def do_prepare_partition(cls, part, source_params, creator, cr_workdir,
                             oe_builddir, bootimg_dir, kernel_dir,
                             rootfs_dir, native_sysroot):
        """
        Called to do the actual content population for a partition i.e. it
        'prepares' the partition to be incorporated into the image.
        In this case, prepare content for legacy bios boot partition.
        """
        bootimg_dir = cls._get_bootimg_dir(bootimg_dir, 'syslinux')

        staging_kernel_dir = kernel_dir

        hdddir = "%s/hdd/boot" % cr_workdir

        kernel = get_bitbake_var("KERNEL_IMAGETYPE")
        if get_bitbake_var("INITRAMFS_IMAGE_BUNDLE") == "1":
            if get_bitbake_var("INITRAMFS_IMAGE"):
                kernel = "%s-%s.bin" % \
                    (get_bitbake_var("KERNEL_IMAGETYPE"), get_bitbake_var("INITRAMFS_LINK_NAME"))

        install_cmd = "install -m 0644 %s/%s %s/%s" % (staging_kernel_dir, kernel, hdddir, get_bitbake_var("KERNEL_IMAGETYPE"))
        exec_cmd(install_cmd)

        try:
            if source_params['loader-pcbios'] == 'grub':
                cls.do_prepare_grub(part, hdddir, bootimg_dir, staging_kernel_dir)
            elif source_params['loader-pcbios'] == 'syslinux':
                cls.do_prepare_syslinux(part, hdddir, bootimg_dir, staging_kernel_dir)
            else:
                raise WicError("unrecognized bootimg-pcbios loader: %s" % source_params['loader-pcbios'])
        except KeyError:
            raise WicError("bootimg-pcbios requires a loader, none specified")

        du_cmd = "du -bks %s" % hdddir
        out = exec_cmd(du_cmd)
        blocks = int(out.split()[0])

        extra_blocks = part.get_extra_block_count(blocks)

        if extra_blocks < BOOTDD_EXTRA_SPACE:
            extra_blocks = BOOTDD_EXTRA_SPACE

        blocks += extra_blocks

        logger.debug("Added %d extra blocks to %s to get to %d total blocks",
                     extra_blocks, part.mountpoint, blocks)

        # dosfs image, created by mkdosfs
        bootimg = "%s/boot%s.img" % (cr_workdir, part.lineno)

        label = part.label if part.label else "boot"

        dosfs_cmd = "mkdosfs -n %s -i %s -S 512 -C %s %d" % \
                    (label, part.fsuuid, bootimg, blocks)
        exec_native_cmd(dosfs_cmd, native_sysroot)

        mcopy_cmd = "mcopy -i %s -s %s/* ::/" % (bootimg, hdddir)
        exec_native_cmd(mcopy_cmd, native_sysroot)

        if source_params['loader-pcbios'] == 'syslinux':
            syslinux_cmd = "syslinux %s" % bootimg
            exec_native_cmd(syslinux_cmd, native_sysroot)

        chmod_cmd = "chmod 644 %s" % bootimg
        exec_cmd(chmod_cmd)

        du_cmd = "du -Lbks %s" % bootimg
        out = exec_cmd(du_cmd)
        bootimg_size = out.split()[0]

        part.size = int(bootimg_size)
        part.source_file = bootimg
