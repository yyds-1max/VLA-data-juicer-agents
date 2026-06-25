#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

import argparse
import time
import os
import numpy as np
import shutil
import math
import multiprocessing
import json

# 默认话题映射表；运行时可通过 --topic_map 或 --topic_map_file 覆盖。
DEFAULT_TOPIC_MAP = {
    # "cam_video2": "front",
    # "cam_video4": "fisheye_left",
    # "cam_video5": "fisheye_right",
    # "cam_video6": "fisheye_back",
    "cam_video5": "fisheye_front",
    "lidar_points":"r32_rslidar_points",  
    "sport_odom":"odom"
}


def _load_json_dict(value, label):
    payload = json.loads(value)
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(child, str) for key, child in payload.items()
    ):
        raise ValueError(f"{label} must be a JSON object with string keys and values")
    return payload


def resolve_topic_map(topic_map, topic_map_file):
    if topic_map_file is not None:
        with open(topic_map_file, "r") as f:
            return _load_json_dict(f.read(), "--topic_map_file")
    if topic_map is not None:
        return _load_json_dict(topic_map, "--topic_map")
    raise ValueError("missing required topic map; pass --topic_map or --topic_map_file")


def string2time(name):
    """
    通过文件名，获取时间戳
    """
    (time, _) = os.path.splitext(name)
    return float(time)

def get_seq_name(seq_index, opt):
    """根据是否提供 sequence_prefix 决定 sequence 目录名"""
    if opt.sequence_prefix:
        return f"{opt.sequence_prefix}_{seq_index}"
    else:
        return str(seq_index)

def copy_sync_data(cur_search_file_indexs_value, search_file_names, cur_search_file_indexs_key, 
                   tmp_data_path, sync_data_path, opt, active_topic_map):
    mapped_dir = active_topic_map.get(cur_search_file_indexs_key, cur_search_file_indexs_key)

    for i, search_file_index in enumerate(cur_search_file_indexs_value):
        seq_index = int(i / opt.max_file_num_in_one_dir)
        seq_name = get_seq_name(seq_index, opt)
        sequence = seq_name + '/'

        id_in_seq = i
        cur_file_name = search_file_names[cur_search_file_indexs_key][search_file_index]
        _, ext = os.path.splitext(cur_file_name)

        if i % 5 == 0:  # 5
            src = os.path.join(tmp_data_path, cur_search_file_indexs_key, cur_file_name)
            dst = os.path.join(sync_data_path, sequence, mapped_dir, str(id_in_seq).zfill(6) + ext)
            shutil.copyfile(src, dst)

def sync_data(opt, active_topic_map=None):
    """
    对数据进行时间同步
    """
    if not active_topic_map:
        raise ValueError("topic_map is required")
    start_time = time.time()
    data_path = opt.data_path
    query_dir = opt.query_dir
    output_dir = opt.output_dir
    # print("开始进行数据时间同步！")
    # 数据目录下的tmp_dir为未同步的数据
    tmp_data_path = os.path.join(data_path, "tmp_dir")
    # 同步后的数据存储目录
    sync_data_path = os.path.join(data_path, output_dir)
    if not os.path.exists(sync_data_path):
        os.makedirs(sync_data_path)
    # 获取当前目录下的所有文件夹(不包含要查询的文件夹)
    search_dir_list = os.listdir(tmp_data_path)

    # 获取要查询的文件夹下的所有文件，并进行排序
    query_file_names = os.listdir(os.path.join(tmp_data_path, query_dir))
    query_file_names = np.array(sorted(query_file_names))
    query_file_times = np.array(list(map(string2time, query_file_names)))

    query_file_indexs = np.arange(len(query_file_names))

    search_file_indexs = {}
    search_file_names = {}
    # 遍历所有要同步的文件夹
    for search_dir in search_dir_list:
        cur_search_file_names = os.listdir(os.path.join(tmp_data_path, search_dir))
        cur_search_file_names = np.array(sorted(cur_search_file_names))
        cur_search_file_times = np.array(list(map(string2time, cur_search_file_names)))

        search_file_names[search_dir] = cur_search_file_names
        cur_search_file_index = []

        for i, query_file_time in enumerate(query_file_times):
            min_del_time_index = np.argmin(abs(query_file_time - cur_search_file_times))
            if abs(cur_search_file_times[min_del_time_index] - query_file_time) > 0.1:  # 0.05
                query_file_indexs[i] = -1
            cur_search_file_index.append(min_del_time_index)

        search_file_indexs[search_dir] = cur_search_file_index

    valid = query_file_indexs > -1

    for cur_search_file_indexs_key in search_file_indexs.keys():
        cur_search_file_indexs_value = np.array(search_file_indexs[cur_search_file_indexs_key])
        search_file_indexs[cur_search_file_indexs_key] = cur_search_file_indexs_value[valid]

    query_file_indexs = query_file_indexs[valid]
    # print("同步前处理耗时: {:.2f}秒".format(time.time() - start_time))

    sequence_cnt = math.ceil(query_file_indexs.shape[0] / opt.max_file_num_in_one_dir)

    totle_time_dic = {}
    for i in range(sequence_cnt):
        seq_name = get_seq_name(i, opt)

        # 在同步后数据存储目录下每个sequence中创建同样的目录结构
        for search_dir in search_dir_list:
            mapped_dir = active_topic_map.get(search_dir, search_dir)
            search_dir_path = os.path.join(sync_data_path, seq_name, mapped_dir)
            if not os.path.exists(search_dir_path):
                os.makedirs(search_dir_path)

        # 保存time.json
        time_dic = {}
        for j in range(i * opt.max_file_num_in_one_dir, 
                       i * opt.max_file_num_in_one_dir + opt.max_file_num_in_one_dir):
            if j >= len(query_file_indexs):
                break
            cur_file_name = query_file_names[query_file_indexs[j]]
            file_time, ext = os.path.splitext(cur_file_name)

            time_dic.update({str(j).zfill(6): file_time})
            totle_time_dic.update({str(j).zfill(6): file_time})

        with open(os.path.join(sync_data_path, seq_name, 'times.json'), 'w') as f:
            json.dump(time_dic, f, indent=4)

    with open(os.path.join(sync_data_path, "times.json"), "w") as f:
        json.dump(totle_time_dic, f, indent=4)

    # 以下为 pose/chassis 注释块（保持原样不动）
    # 存储同步后的pose数据到pose.txt内
    # ...
    # 存储同步后的chassis数据到chassis.txt内
    # ...

    # 遍历所有要同步的数据文件夹，并将同步后的数据复制到对应的文件夹内
    pool = multiprocessing.Pool(processes=opt.processes_num)
    for cur_search_file_indexs_key in search_file_indexs.keys():
        cur_search_file_indexs_value = np.array(search_file_indexs[cur_search_file_indexs_key])
        copy_sync_data(cur_search_file_indexs_value, search_file_names, cur_search_file_indexs_key,
                       tmp_data_path, sync_data_path, opt, active_topic_map)
    pool.close()
    pool.join()

    end_time = time.time()
    print("时间同步与拆包结束！, ", "耗时: {:.2f}秒".format(end_time - start_time))


    # 将数字索引的文件名修改为对应时间戳命名
    root_timesjson = os.path.join(sync_data_path, "times.json")
    with open(root_timesjson, "r") as f:
        times_dict = json.load(f)
    # 遍历A下所有clip文件夹
    for clip_name in os.listdir(sync_data_path):
        clip_path = os.path.join(sync_data_path, clip_name)
        if not os.path.isdir(clip_path):
            continue
        # 遍历clip下所有传感器文件夹
        for sensor_name in os.listdir(clip_path):
            sensor_path = os.path.join(clip_path, sensor_name)
            if not os.path.isdir(sensor_path):
                continue
            # 遍历传感器文件夹下所有文件
            for file_name in os.listdir(sensor_path):
                file_path = os.path.join(sensor_path, file_name)
                if not os.path.isfile(file_path):
                    continue
                # 文件名前6位是数字索引
                prefix = file_name[:6]
                ext = os.path.splitext(file_name)[1]
                # 找到对应的时间戳
                if prefix in times_dict:
                    new_name = f"{times_dict[prefix]}{ext}"
                    new_path = os.path.join(sensor_path, new_name)
                    os.rename(file_path, new_path)
                    # print(f"{file_path} -> {new_path}")
                else:
                    print(f"警告: {prefix} 未在 times.json 中找到")
    print("文件已修改为时间戳形式！   数据处理结束！")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='data studio', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--data_path', type=str, default=None, help='data path')
    parser.add_argument('--query_dir', type=str, default=None, help='base file to matching')
    parser.add_argument('--output_dir', type=str, default=None, help='output dir')
    parser.add_argument('--sequence_prefix', type=str, default="", help='sequence prefix')
    parser.add_argument('--max_file_num_in_one_dir', type=int, default=600, help='max_file_num_in_one_dir')
    parser.add_argument('--processes_num', type=int, default=4, help='processes num')
    parser.add_argument('--topic_map', type=str, default=None, help='JSON topic map')
    parser.add_argument('--topic_map_file', type=str, default=None, help='JSON topic map file')
    opt = parser.parse_args()
    if opt.topic_map is None and opt.topic_map_file is None:
        parser.error("one of --topic_map or --topic_map_file is required")
    sync_data(opt, resolve_topic_map(opt.topic_map, opt.topic_map_file))
