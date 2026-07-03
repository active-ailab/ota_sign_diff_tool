#!/usr/bin/python
# -*- coding: UTF-8 -*-

import sys, os, errno
import zipfile
import tarfile
import re
import ast

import ecdsa
from hashlib import sha256
import json
import tempfile
import io
import shutil
import ctypes
from pathlib import Path

'''
RegisterFunction("package_extract_dir", package_extract_dir_fn);
RegisterFunction("package_extract_file", package_extract_file_fn);
RegisterFunction("write_image", write_image_fn);
RegisterFunction("mkdir", mkdir_fn);
RegisterFunction("rm", rm_fn);
RegisterFunction("rename", rename_fn);
RegisterFunction("set_recovery_on", set_recovery_on_fn);
RegisterFunction("version_check", version_check_fn);
RegisterFunction("diff_check", diff_check_fn);
RegisterFunction("mkfs", mkfs_fn);
RegisterFunction("bspatch", bspatch_fn);
'''

def bsdiff_patch_apply(old_zfile, diff_zfile, file_path, patch_path):
    # Load the shared library (assuming it's named bspatch.so and located in the same directory)
    libbspatch = ctypes.CDLL(os.path.dirname(os.path.abspath(__file__)) + "/lib/libbspatch.so")
    content = b''

    # Define the function prototype
    bspatch_apply = libbspatch.bspatch_apply
    bspatch_apply.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    bspatch_apply.restype = ctypes.c_int

    # 解压到临时文件夹
    with tempfile.TemporaryDirectory() as temp_dir:
        old_file_dir = os.path.join(temp_dir, 'oldfile')
        new_file_name = os.path.join(temp_dir, 'newfile')
        patch_file_dir = os.path.join(temp_dir, 'patchfile')

        old_zfile.extract(file_path, old_file_dir)
        diff_zfile.extract(patch_path, patch_file_dir)

        old_file_name = os.path.join(old_file_dir, file_path)
        patch_file_name = os.path.join(patch_file_dir, patch_path)

        result = bspatch_apply(old_file_name.encode("utf-8"), new_file_name.encode("utf-8"), patch_file_name.encode("utf-8"))

        if result == 0:
            with open(new_file_name, 'rb') as f:
                content = f.read()
                print("Patch apply successfully:", file_path)
        else:
            print("Error patch apply file:", file_path)

    return content

def sha256sum(filename):
    h  = sha256()
    with open(filename, 'rb') as f:
        content = f.read()
        h.update(content)
        return h.hexdigest()
    os.exit(255)
    return ''

def ota_diff_apply(old_archive, diff_archive, output_path, ignore_list):
    apply_result = True
    ignore_set = set(os.path.normpath(p) for p in (ignore_list or []))
    command_pattern = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\((.*)\)\s*;?\s*$')

    def _parse_args(args_text):
        args_text = args_text.strip()
        if not args_text:
            return []
        try:
            parsed = ast.literal_eval('[' + args_text + ']')
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass

        # 兜底：保持与历史逻辑兼容
        parsed_arguments = []
        for arg in args_text.split(','):
            arg = arg.strip()
            if arg.isdigit():
                parsed_arguments.append(int(arg))
            elif arg.lower() == 'true' or arg.lower() == 'false':
                parsed_arguments.append(arg.lower() == 'true')
            else:
                parsed_arguments.append(arg.replace('"',''))
        return parsed_arguments

    # 创建临时文件夹
    with tempfile.TemporaryDirectory() as temp_dir:
        # 解压旧全量包
        with zipfile.ZipFile(old_archive, 'r', zipfile.ZIP_DEFLATED) as old_zfile:
            old_zfile.extractall(path=temp_dir, members=[member for member in old_zfile.namelist() if member.startswith('META')])

        # 应用差分包
        with zipfile.ZipFile(diff_archive, 'r', zipfile.ZIP_DEFLATED) as diff_zfile:
            update_script = diff_zfile.read('update-script').decode('utf-8')
            memory_file = io.StringIO(update_script)
            
            for line in memory_file:
                #解析指令
                command_string = line.strip()
                if not command_string:
                    continue

                m = command_pattern.match(command_string)
                if not m:
                    print("无法解析命令字符串：", line)
                    continue

                function_name = m.group(1)
                parsed_arguments = _parse_args(m.group(2))

                print(command_string)

                # 升级指令处理
                if function_name == 'package_extract_dir':
                    src_dir = parsed_arguments[0]
                    dst_dir = parsed_arguments[1].replace('/mnt/system', temp_dir)
                    diff_zfile.extractall(path=dst_dir, members=[member for member in diff_zfile.namelist() if member.startswith(src_dir)])

                elif function_name == 'package_extract_file':
                    src_file = parsed_arguments[0]
                    dst_file = parsed_arguments[1].replace('/mnt/system', temp_dir + '/META')
                    directory_name = os.path.dirname(dst_file)
                    if directory_name and not os.path.exists(directory_name):
                        os.makedirs(directory_name)
                    with open(dst_file, 'wb') as fw:
                        fw.write(diff_zfile.read(src_file))

                elif function_name == 'write_image':
                    None
                elif function_name == 'mkdir':
                    dirname = parsed_arguments[0].replace('/mnt/system', temp_dir + '/META')
                    if not os.path.exists(dirname):
                        os.mkdir(dirname)

                elif function_name == 'rm':
                    rm_file = parsed_arguments[0].replace('/mnt/system', temp_dir + '/META')
                    if os.path.isfile(rm_file):
                        os.remove(rm_file)
                    elif os.path.isdir(rm_file):
                        shutil.rmtree(rm_file)

                elif function_name == 'set_recovery_on':
                    None
                elif function_name == 'version_check':
                    None
                elif function_name == 'diff_check':
                    None
                elif function_name == 'mkfs':
                    None
                elif function_name == 'bspatch':
                    with zipfile.ZipFile(old_archive, 'r', zipfile.ZIP_DEFLATED) as old_zfile:
                        old_file = parsed_arguments[0].replace('/mnt/system', 'META')
                        path_file = parsed_arguments[1]
                        new_conent = bsdiff_patch_apply(old_zfile, diff_zfile, old_file, path_file)
                        if new_conent:
                            # 创建文件
                            file_name = os.path.join(temp_dir, old_file)
                            with open(file_name, "wb") as file:
                                file.write(new_conent)
                else:
                    print("不支持该指令")
                    print("函数名:", function_name)
                    print("参数:", parsed_arguments)

        # 拷贝到输出文件夹
        if output_path:
            shutil.rmtree(output_path)
            shutil.copytree(temp_dir, output_path)

        # 文件列表hash校验
        with open(temp_dir + '/META/filehash', 'r') as file:
            content = file.read()
        flist = json.loads(content)

        for (filename, filehash) in flist.items():
            filename = os.path.normpath(filename)

            if filename in ignore_set:
                continue

            filename = temp_dir + '/' + filename

            if not Path(filename).exists():
                print('file {0} not found'.format(filename))
                apply_result = False
            elif sha256sum(filename) != filehash:
                print('filename : {0} hash error'.format(filename))
                apply_result = False

    print("diff apply result:", apply_result)

    return apply_result

if __name__ == '__main__':
    if len(sys.argv) != 4 and len(sys.argv) != 3:
        print("Usage: {bin} <old_archive> <diff archive> [output path]".format(bin=os.path.basename(sys.argv[0])))
        sys.exit(-1)

    old_archive  = sys.argv[1]
    diff_archive = sys.argv[2]

    if len(sys.argv) == 4:
        output_path = sys.argv[3]
    else:
        output_path = None

    ignore_list = ['META/bootloader_sign.bin', 'META/extfw/recovery_sign.bin']

    if ota_diff_apply(old_archive, diff_archive, output_path, ignore_list) == False:
        sys.exit(-1)