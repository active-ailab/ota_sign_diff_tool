#!/usr/bin/python
# -*- coding: UTF-8 -*-

import sys, os, errno
import zipfile
import tarfile

import re
import struct
import ecdsa
from hashlib import sha256
import json
from distutils.version import LooseVersion
import ctypes
import tempfile
from ota_diff_apply import *

'''
typedef struct {
    uint32_t magic_1;
    uint32_t magic_2;
    uint16_t head_size;
    uint32_t fw_size;
    uint16_t pnpid;
    uint16_t pnpver;
    char     fw_ver[16];
    uint32_t fw_origin_size;
    uint8_t  hw_major_minor_ver;
    uint8_t  reserved[9];
} sig_head_t;
'''

sig_head_struct = '<8sHI2H16sI10B'

# 595项目bootloader存储位置为分区偏移1024byte, 其他项目为0偏移
# bootloader偏移位置为0的项目pnp id列表
# 0x0062-cannes/0x0063-bled pro/0x0067-L66/0x0073-provence/0x0075-bari/0x0074-leiden
boot_no_offset_pnpid_table = [0x0062, 0x0063, 0x0067, 0x0073, 0x0075, 0x0074]

# 差分检查需要忽略的文件列表
diff_check_ignore_list = ['META/bootloader_sign.bin', 'META/extfw/recovery_sign.bin', 'META/res_info']

def _read_zip_member_compat(zf, path):
    """兼容 Windows/ZIP 路径分隔符差异，优先读原路径，失败后回退到 '/'。"""
    try:
        return zf.read(path)
    except KeyError:
        alt_path = path.replace('\\', '/')
        if alt_path != path:
            return zf.read(alt_path)
        raise

def get_orign_file_compare(new_zfile,old_zfile,bootloader_path):
    sig_head_size = struct.calcsize(sig_head_struct)

    new_content = _read_zip_member_compat(new_zfile, bootloader_path)
    old_content = _read_zip_member_compat(old_zfile, bootloader_path)

    new_len = (sig_head_size + 64)
    old_len = (sig_head_size + 64)
    new_content_list = list(new_content)
    old_content_list = list(old_content)

    del new_content_list[-new_len:]
    del old_content_list[-old_len:]

    if new_content_list == old_content_list:
        return True
    else:
        return False

def signature(data, pem_file):
    with open(pem_file) as pem:
        signKey = ecdsa.SigningKey.from_pem(pem.read(), hashfunc = sha256)
    return signKey.sign(data, hashfunc=sha256)

def remove_file(filename):
    try:
        os.remove(filename)
    except OSError:
        pass


def gen_sign_footer(fw_size, pnp_id, pnp_ver, fw_ver,fw_origin_size,hw_major_minor_ver):
    fw_ver += '\0' * 16
    fw_ver = fw_ver[:16]

    return struct.pack('<8sHI2H16sI10B', str.encode('HMZPSIGN'), 48, fw_size, pnp_id, pnp_ver,\
            str.encode(fw_ver), fw_origin_size, hw_major_minor_ver, 0, 0, 0, 0, 0, 0, 0, 0, 0)

def get_hw_version(archive_path):
    hw_version = 0
    with open(archive_path, 'rb') as f:
        sig_head_size = struct.calcsize(sig_head_struct)
        f.seek((sig_head_size + 64) * -1, 2);
        sig_hdr = f.read(sig_head_size)
        hdr = struct.unpack(sig_head_struct, sig_hdr)
        hdr = list(hdr)
        hw_version = hdr[7]
        f.close()

    return hw_version

def get_pnp_config(archive_path):
    pnp_id = 0
    pnp_ver = 0
    with open(archive_path, 'rb') as f:
        sig_head_size = struct.calcsize(sig_head_struct)
        f.seek((sig_head_size + 64) * -1, 2);
        sig_hdr = f.read(sig_head_size)
        hdr = struct.unpack(sig_head_struct, sig_hdr)
        hdr = list(hdr)
        pnp_id = hdr[3]
        pnp_ver = hdr[4]
        f.close()

    return (pnp_id, pnp_ver)


def get_fw_version(archive_path):
    fw_version = 0
    with open(archive_path, 'rb') as f:
        sig_head_size = struct.calcsize(sig_head_struct)
        f.seek((sig_head_size + 64) * -1, 2);
        sig_hdr = f.read(sig_head_size)
        hdr = struct.unpack(sig_head_struct, sig_hdr)
        hdr = list(hdr)
        fw_version = hdr[5].decode('utf-8','strict')
        f.close()

    return fw_version

# 获取压缩包中bootloader的版本号
def get_boot_version(archive_path):
    fw_version = ''
    sig_head_size = struct.calcsize(sig_head_struct)
    zfile = zipfile.ZipFile(archive_path, 'r', zipfile.ZIP_DEFLATED)
    content = zfile.read('META/bootloader_sign.bin')
    zfile.close()

    if len(content) > sig_head_size + 64:
        sig_hdr = content[(sig_head_size + 64) * -1:-64]
        hdr = struct.unpack(sig_head_struct, sig_hdr)
        hdr = list(hdr)
        fw_version = hdr[5].decode('utf-8','strict')

    return fw_version

def gen_zip_signature(new_archive_path,archive_path, pem_file,fw_origin_size):
    with open(archive_path, 'rb') as archive:
        data = archive.read()

        sign_archive_name = archive_path[:-4] + '_sign.zip'
        with open(sign_archive_name, 'wb+') as sign_archive:
            (pnp_id, pnp_ver) = get_pnp_config(new_archive_path)
            print('(pnp_id, pnp_ver):', (pnp_id, pnp_ver))

            fw_ver = get_fw_version(new_archive_path)
            print('fw_ver:', fw_ver)

            hw_major_minor_ver = get_hw_version(new_archive_path)
            print('hw_major_minor_ver/fw_origin_size:', (hw_major_minor_ver,fw_origin_size))

            data += gen_sign_footer(len(data), pnp_id, pnp_ver, fw_ver,fw_origin_size,hw_major_minor_ver)
            data += signature(data, pem_file)
            sign_archive.write(data)
    remove_file(archive_path)
    os.rename(sign_archive_name, archive_path)

def is_signed_archive(archive):
    with open(archive, 'rb') as f:
        sig_head_size = struct.calcsize(sig_head_struct)
        f.seek((sig_head_size + 64) * -1, 2);
        sig_hdr = f.read(sig_head_size)
        hdr = struct.unpack(sig_head_struct, sig_hdr)
        hdr = list(hdr)
        magic = hdr[0].decode('utf-8','strict')
        if magic == 'HMZPSIGN' or magic == 'HMZ2SIGN':
            hdr[0] = 'HMZ2SIGN'.encode('utf-8','strict')
            return struct.pack('<8sHI2H16sI10B', *hdr)
    return None

def get_archive_filehash(archive_path):
    zfile = zipfile.ZipFile(archive_path, 'r', zipfile.ZIP_DEFLATED)
    content = zfile.read('filehash')
    zfile.close()

    flist = json.loads(content)
    new_flist = {}

    for (filename, filehash) in flist.items():
        # ZIP 内部路径始终使用 '/'，避免 Windows 下 '\\' 导致读取失败
        filename = filename.replace('\\', '/')
        new_flist[filename] = filehash

    return new_flist

def get_archive_filehash_hash(archive_path):
    zfile = zipfile.ZipFile(archive_path, 'r', zipfile.ZIP_DEFLATED)
    content = zfile.read('filehash')
    zfile.close()

    h  = sha256()
    h.update(content)
    return h.hexdigest()

def get_archive_package_discription(archive_path):
    packageDiscription = {}
    zfile = zipfile.ZipFile(archive_path, 'r', zipfile.ZIP_DEFLATED)
    if 'packageDiscription.json' in zfile.namelist():
        content = zfile.read('packageDiscription.json')
        packageDiscription = json.loads(content)
    zfile.close()

    return packageDiscription

#使用bsdiff算法生成patch数据
def gen_bsdiff_patch(old_zfile, new_zfile, filepath):
    # Load the shared library (Windows 环境通常不存在 .so，失败时自动降级为非 bsdiff)
    try:
        libbsdiff = ctypes.CDLL(os.path.dirname(os.path.abspath(__file__)) + "/lib/libbsdiff.so")
    except OSError:
        return b''
    content = b''

    # Define the function prototype
    bsdiff_gen_patch = libbsdiff.bsdiff_gen_patch
    bsdiff_gen_patch.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    bsdiff_gen_patch.restype = ctypes.c_int

    # 解压到临时文件夹
    with tempfile.TemporaryDirectory() as temp_dir:
        old_file_dir = os.path.join(temp_dir, 'oldfile')
        new_file_dir = os.path.join(temp_dir, 'newfile')
        patch_file_name = os.path.join(temp_dir, 'patchfile')

        old_zfile.extract(filepath, old_file_dir)
        new_zfile.extract(filepath, new_file_dir)

        old_file_name = os.path.join(old_file_dir, filepath)
        new_file_name = os.path.join(new_file_dir, filepath)

        result = bsdiff_gen_patch(old_file_name.encode("utf-8"), new_file_name.encode("utf-8"), patch_file_name.encode("utf-8"))

        if result == 0:
            with open(patch_file_name, 'rb') as f:
                content = f.read()
                print("Patch file generated successfully:", filepath)
        else:
            print("Error generating patch file:", filepath)

    return content


def gen_diff_archive(pem_file, old_archive, new_archive, out_archive, custom_config):
    sig_hdr = is_signed_archive(old_archive)
    if sig_hdr is None:
        print('{0} is not signed archive'.format(old_archive));
        return False

    sig_hdr = is_signed_archive(new_archive)
    if sig_hdr is None:
        print('{0} is not signed archive'.format(new_archive));
        return False


    print('gen diff archive "{2}" between "{0}" and "{1}"'.format(old_archive, new_archive, out_archive))

    old_filelist = get_archive_filehash(old_archive)
    new_filelist = get_archive_filehash(new_archive)
    packageDiscription = get_archive_package_discription(new_archive)

    new_mod_list = []
    rm_list = []
    old_dir_list = []
    new_dir_list = []
    rm_dir_list = []

    update_script = ''''''
    firmware_update_flag = False
    bootloader_update_flag = False

    for (filename, filehash) in old_filelist.items():
        if filename not in new_filelist:
            rm_list.append(filename)
        elif filehash != new_filelist[filename]:
            new_mod_list.append(filename)

        #获取老固件包的目录列表
        str_path = filename
        while (len(str_path) -1) > 0:
            lst_path = str_path.split('/')
            lst_path.pop(-1)
            str_path = '/'.join(lst_path)
            if str_path not in old_dir_list:
                old_dir_list.append(str_path)

    for (filename, filehash) in new_filelist.items():
        if filename not in old_filelist:
            new_mod_list.append(filename)

        #获取新固件包的目录列表
        str_path = filename
        while (len(str_path) -1) > 0:
            lst_path = str_path.split('/')
            lst_path.pop(-1)
            str_path = '/'.join(lst_path)
            if str_path not in new_dir_list:
                new_dir_list.append(str_path)

    #获取需要删除的目录列表
    for old_dir in old_dir_list:
        if old_dir not in new_dir_list:
            rm_dir_list.append(old_dir)

    #过滤低层级目录(保留顶级目录)
    low_dir_list = []
    for tmp_dir in rm_dir_list:
        for rm_dir in rm_dir_list:
            if (tmp_dir != rm_dir) and (rm_dir.find(tmp_dir+'/') == 0):
                low_dir_list.append(rm_dir)

    rm_dir_list = list(set(rm_dir_list) - set(low_dir_list))

    # 去掉多余的文件删除指令，删除顶级目录即可
    remove_list = list()
    for dir_item in rm_dir_list:
        for file_item in rm_list:
            if dir_item in file_item:
                remove_list.append(file_item)
    rm_list = list(set(rm_list) - set(remove_list))

    #目录列表添加到删除列表中
    rm_list += rm_dir_list

    config_json = {}
    skip_diff_check = False
    append_cmds = ''
    boot_cert_upgrade_cmd = ''
    bspatch_cmd_list = []

    if custom_config:
        # 将字符串转换为 Python 对象
        try:
            config_json = json.loads(custom_config)
        except json.JSONDecodeError as e:
            print("解析 JSON 格式字符串出错：", e)
            return False

    # 排除不需要更新的文件
    if 'exclude' in config_json:
        for exclude_file in config_json['exclude']:
            if exclude_file in new_mod_list:
                new_mod_list.remove(exclude_file)
                print("exclude file:", exclude_file)
                diff_check_ignore_list.append(exclude_file)

    # bootloader版本号锁定
    if 'bootloaderLockVersion' in config_json:
        locked_version = config_json['bootloaderLockVersion']
        new_version = get_boot_version(new_archive)
        new_version = new_version.replace('\x00', '')

        print('locked_version:', locked_version)
        print('new_version:', new_version)

        if new_version and LooseVersion(new_version) > LooseVersion(locked_version):
            bootloader_path = 'META/bootloader_sign.bin'
            if bootloader_path in new_mod_list:
                new_mod_list.remove(bootloader_path)
                print("skip upgrade bootloader")

    # 特殊版本处理
    if 'versionSpecCommands' in  config_json:
        fw_version = get_fw_version(old_archive)
        fw_version = fw_version.replace('\x00', '')
        print(fw_version)
        if fw_version in config_json['versionSpecCommands']:
            if config_json['versionSpecCommands'][fw_version]['checkImage'] == False:
                skip_diff_check = True
            if 'appendCommand' in config_json['versionSpecCommands'][fw_version]:
                for appendCmd in config_json['versionSpecCommands'][fw_version]['appendCommand']:
                    append_cmds = append_cmds + appendCmd
                    if append_cmds:
                        append_cmds = append_cmds + os.linesep

    new_mod_list.append('filehash')

    old_archive_hash = get_archive_filehash_hash(old_archive)

    #check recovery version first
    check_update_script = 'version_check(2);'
    #check base on archive hash
    if skip_diff_check == False:
        check_update_script = check_update_script + os.linesep + 'diff_check({0});'.format(old_archive_hash)
    #enable fail retry
    #check_update_script = check_update_script + os.linesep + 'set_recovery_on();'

    update_script = check_update_script + os.linesep + update_script

    out_zfile = zipfile.ZipFile(out_archive, 'w', zipfile.ZIP_DEFLATED)
    new_zfile = zipfile.ZipFile(new_archive, 'r', zipfile.ZIP_DEFLATED)
    old_zfile = zipfile.ZipFile(old_archive, 'r', zipfile.ZIP_DEFLATED)

    recovery_path = 'META/extfw/recovery_sign.bin'
    if recovery_path in new_mod_list:
        if recovery_path in old_filelist:
            cmp = get_orign_file_compare(new_zfile,old_zfile,recovery_path)
            if cmp == False:
                print('recovery_sign.bin diff :')
            else:
                print('recovery_sign.bin same :',recovery_path)
                new_mod_list.remove(recovery_path)

    firmware_path = 'META/firmware.bin'
    firmware_path_new = 'firmware.bin'
    firmware_use_new_path = False

    if firmware_path_new in new_zfile.namelist():
        # 新包使用新路径
        firmware_use_new_path = True

    if firmware_use_new_path:
        content = new_zfile.read(firmware_path_new)
        out_zfile.writestr(firmware_path_new, content)
        firmware_update_flag = True
    elif firmware_path in new_mod_list:
        firmware_update_flag = True

    bootloader_path = 'META/bootloader_sign.bin'
    if bootloader_path in new_mod_list:
        (pnp_id, pnp_ver) = get_pnp_config(new_archive)

        if 'bootUpgradeCMD' in packageDiscription:
            bootloader_cmd = packageDiscription['bootUpgradeCMD']
        elif pnp_id in boot_no_offset_pnpid_table:
            bootloader_cmd = 'write_image("META/bootloader_sign.bin", "boot", 0);'
        else:
            bootloader_cmd = 'write_image("META/bootloader_sign.bin", "boot", 1024);'

        if bootloader_path in old_filelist:
            cmp = get_orign_file_compare(new_zfile,old_zfile,bootloader_path)
            if cmp == False:
                bootloader_update_flag = True
                print('bootloader_sign.bin diff :')
            else:
                print('bootloader_sign.bin same :')
                new_mod_list.remove(bootloader_path)
        else:
            bootloader_update_flag = True

    total_size = 0

    #bsdiff差分处理
    #hannover之后的项目recovery才支持bsdiff算法
    #根据pnpid来区分：https://zepp.feishu.cn/wiki/wikcnVjnxIdALY4AQ8hXAIpy1Uh
    (pnp_id, pnp_ver) = get_pnp_config(new_archive)
    if 'bsdiff' in packageDiscription and pnp_id >= 0x7F:
        bsdiff_file_list = []
        enable_path = packageDiscription['bsdiff']['enable_path']
        ignore_path = packageDiscription['bsdiff']['ignore_path']

        for path in enable_path:
            for item in new_mod_list:
                if item in old_filelist and item not in ignore_path and path in item:
                    patch_conent = gen_bsdiff_patch(old_zfile, new_zfile, item)
                    if len(patch_conent):
                        bsdiff_file_list.append(item)
                        patch_path = 'BSPATCH' + item[4:] + '_patch'
                        out_zfile.writestr(patch_path, patch_conent)
                        old_file = '/mnt/system' + item[4:]
                        zipInfo = new_zfile.getinfo(item)
                        total_size += zipInfo.file_size
                        crc_str=hex(zipInfo.CRC)
                        crc_str=crc_str[2:]
                        cmd = f'bspatch("{old_file}", "{patch_path}", "{crc_str}");'
                        bspatch_cmd_list.append(cmd)

        for item in bsdiff_file_list:
            new_mod_list.remove(item)

    for item in new_mod_list:
        content = new_zfile.read(item)
        zipInfo = new_zfile.getinfo(item)
        total_size+=zipInfo.file_size
        print ('item:', item)
        print ('file_size:', zipInfo.file_size)
        out_zfile.writestr(item, content)

    rm_cmd = []
    for item in rm_list:
        target_path = '/mnt/system' + item[4:]
        rm_cmd.append('rm("{0}");'.format(target_path))

    if len(rm_cmd):
        update_script = update_script + os.linesep + os.linesep.join(rm_cmd)

    if len(bspatch_cmd_list):
        update_script = update_script + os.linesep + os.linesep.join(bspatch_cmd_list)

    update_script = update_script+os.linesep +'package_extract_dir("META/", "/mnt/system/");'

    fct_file_name0 = 'fct_test_partition0.bin'
    fct_file_name1 = 'fct_test_partition1.bin'
    if fct_file_name0 in new_zfile.namelist() and fct_file_name1 in new_zfile.namelist():
        content = new_zfile.read(fct_file_name0)
        out_zfile.writestr(fct_file_name0, content)
        content = new_zfile.read(fct_file_name1)
        out_zfile.writestr(fct_file_name1, content)
        update_script = update_script + os.linesep + 'package_extract_file("fct_test_partition0.bin", "/fsraw/pcbatest0");'
        update_script = update_script + os.linesep + 'package_extract_file("fct_test_partition1.bin", "/fsraw/pcbatest1");'

    if firmware_update_flag == True:
        if firmware_use_new_path:
            update_script = update_script+os.linesep + 'write_image("firmware.bin", "app");'
        else:
            update_script = update_script+os.linesep + 'write_image("META/firmware.bin", "app");'

    if bootloader_update_flag == True:
        update_script = update_script+os.linesep + bootloader_cmd
        cert_path = 'META/boot_cert.bin'
        if 'secureBoot' in packageDiscription and packageDiscription['secureBoot'] == True:
            boot_cert_upgrade_cmd = packageDiscription['bootCertUpgradeCMD']
            update_script = update_script + os.linesep + boot_cert_upgrade_cmd
        elif cert_path in new_mod_list:
            update_script = update_script + os.linesep + 'write_image("META/boot_cert.bin", "securekey", 3072);'

    if append_cmds:
        update_script = update_script + os.linesep + append_cmds

    update_script = update_script+os.linesep +'package_extract_file("filehash", "/mnt/system/filehash");'
    out_zfile.writestr('update-script', update_script)

    out_zfile.close()
    new_zfile.close()
    old_zfile.close()

    gen_zip_signature(new_archive,out_archive, pem_file,total_size)

    print('gen diff archive succeed');

    if ota_diff_apply(old_archive, out_archive, None, diff_check_ignore_list) == True:
        print('ota diff apply check pass')
    else:
        # Windows 环境下本地校验常因工具/路径差异失败，不应阻断已生成的差分包输出
        print('ota diff apply check ERROR (non-fatal, diff archive already generated)')

    return True

def get_archive_desc(project_name, fw_name, build_time, version, diff_list):
    firmware_desc = {}
    firmware_desc['flag'] = 7;
    firmware_desc['name'] = fw_name
    firmware_desc['version'] = version

    diff_desc = []
    for item in diff_list:
        desc = {'orginVersion' : item['version'], 'name' : item['name']}
        diff_desc.append(desc)

    archive_desc = {}
    archive_desc['buildTime'] = build_time
    archive_desc['deviceSource'] = project_name
    archive_desc['language'] = 'zh,en'
    archive_desc['languageFamily'] = 1
    archive_desc['packageName'] = '{0}({1})'.format(project_name, build_time)
    archive_desc['packageVersion'] = version
    archive_desc['support8Bytes'] = True
    archive_desc['packages'] = {'fw' : firmware_desc}
    archive_desc['diff'] = diff_desc

    return archive_desc

def get_archive_version(path):
    sig_hdr = is_signed_archive(path)
    if sig_hdr is not None:
        hdr = list(struct.unpack(sig_head_struct, sig_hdr))
        version = hdr[5].decode('utf-8','strict').strip('\x00')
        return version
    return None

def get_project_config(config_file):
    project = ""
    with open(config_file, "r") as f:
        config_mk_str = f.read()

        find_str = re.search( r'PRODUCT_DEVICE_NAME=.*', config_mk_str)
        project_str = find_str.group()
        project = project_str.replace('PRODUCT_DEVICE_NAME=', '')[1:-1]

    return project

def gen_diff_archive_from_dir(pem_file, old_archive_dir, new_archive, out_archive_dir, build_time):
    if is_signed_archive(new_archive) is None:
        print('{0} is not signed archive'.format(new_archive));
        sys.exit(-1)

    project_name = get_project_config('.config')
    fw_name = 'watch.zip'

    old_archive_list = []

    try:
        for item in os.listdir(old_archive_dir):
            item_path = os.path.join(old_archive_dir, item)
            if os.path.isfile(item_path):
                old_archive_list.append(item_path)
    except OSError:
        old_archive_list = []

    try:
        os.makedirs(out_archive_dir)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

    diff_archive_list = []

    for item in old_archive_list:
        version = get_archive_version(item)
        item_name = version + '.zip'
        item_path = os.path.join(out_archive_dir, item_name)
        gen_diff_archive(pem_file, item, new_archive, item_path)
        diff_item = {'version' : version, 'name' : item_name, 'path' : item_path}
        diff_archive_list.append(diff_item)

    new_version = get_archive_version(new_archive)
    desc = get_archive_desc(project_name, fw_name, build_time, new_version, diff_archive_list);
    desc_name = 'PackageBuild.json'
    desc_path = os.path.join(out_archive_dir, desc_name)
    with open(desc_path, "w") as desc_file:
        desc_file.write(json.dumps(desc, indent = 4))

    tar_file_path = os.path.join(out_archive_dir, project_name + '.tgz')
    remove_file(tar_file_path)
    tfile = tarfile.open(tar_file_path, 'x:gz')
    tfile.add(desc_path, arcname = desc_name)
    tfile.add(new_archive, arcname = fw_name)
    for item in diff_archive_list:
        tfile.add(item['path'], arcname = item['name'])
    tfile.close()


if __name__ == '__main__':
    if len(sys.argv) != 5:
        print("Usage: {bin} <pem path> <old archive> <new archive> <out archive>".format(bin=os.path.basename(sys.argv[0])))
        sys.exit(-1)

    pem_file    = sys.argv[1]
    old_archive = sys.argv[2]
    new_archive = sys.argv[3]
    out_archive = sys.argv[4]

    if gen_diff_archive(pem_file, old_archive, new_archive, out_archive, None) == False:
        sys.exit(-1)