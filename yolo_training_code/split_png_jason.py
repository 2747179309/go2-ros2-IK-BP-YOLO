import os
import shutil

# 定义源目录和目标目录（使用双反斜杠或原始字符串，避免转义问题）
source_dir = r'D:\大创\apple_photo'  # 图片和标注都在同一个目录，简化为一个源目录
target_dir = r'D:\大创\数据集'

# 创建目标目录结构
os.makedirs(os.path.join(target_dir, 'images', 'train'), exist_ok=True)
os.makedirs(os.path.join(target_dir, 'images', 'val'), exist_ok=True)
os.makedirs(os.path.join(target_dir, 'labels', 'train'), exist_ok=True)
os.makedirs(os.path.join(target_dir, 'labels', 'val'), exist_ok=True)


# 核心修改：同时获取.jpg和.png格式的图片文件
def get_image_files(dir_path):
    """获取目录下所有.jpg/.png格式的图片文件"""
    image_files = []
    for f in os.listdir(dir_path):
        if f.lower().endswith(('.jpg', '.png')):  # 小写匹配，兼容大写后缀（如.PNG）
            image_files.append(f)
    return sorted(image_files)


# 获取图片文件和json标注文件
image_files = get_image_files(source_dir)
json_files = sorted([f for f in os.listdir(source_dir) if f.endswith('.json')])

# 提取文件名（去掉后缀），用于匹配图片和标注
image_basenames = [os.path.splitext(f)[0] for f in image_files]
json_basenames = [os.path.splitext(f)[0] for f in json_files]

# 只保留有对应标注文件的图片
matching_basenames = set(image_basenames).intersection(set(json_basenames))
# 重新构建匹配的图片/标注文件列表
matched_image_files = [f for f in image_files if os.path.splitext(f)[0] in matching_basenames]
matched_json_files = [f"{name}.json" for name in matching_basenames]

# 按8:2划分训练集和验证集
num_files = len(matched_image_files)
if num_files == 0:
    print("错误：没有找到匹配的图片和标注文件！")
else:
    split_index = int(num_files * 0.8)
    train_image_files = matched_image_files[:split_index]
    val_image_files = matched_image_files[split_index:]

    # 复制训练集文件
    for img_file in train_image_files:
        base_name = os.path.splitext(img_file)[0]
        json_file = f"{base_name}.json"

        # 复制图片到train/images
        src_img = os.path.join(source_dir, img_file)
        dst_img = os.path.join(target_dir, 'images', 'train', img_file)
        shutil.copy(src_img, dst_img)

        # 复制标注到train/labels
        src_json = os.path.join(source_dir, json_file)
        dst_json = os.path.join(target_dir, 'labels', 'train', json_file)
        shutil.copy(src_json, dst_json)

        print(f"已复制训练集：{img_file} + {json_file}")

    # 复制验证集文件
    for img_file in val_image_files:
        base_name = os.path.splitext(img_file)[0]
        json_file = f"{base_name}.json"

        # 复制图片到val/images
        src_img = os.path.join(source_dir, img_file)
        dst_img = os.path.join(target_dir, 'images', 'val', img_file)
        shutil.copy(src_img, dst_img)

        # 复制标注到val/labels
        src_json = os.path.join(source_dir, json_file)
        dst_json = os.path.join(target_dir, 'labels', 'val', json_file)
        shutil.copy(src_json, dst_json)

        print(f"已复制验证集：{img_file} + {json_file}")

    # 输出统计信息
    print(f"\n===== 数据集划分完成 =====")
    print(f"总匹配文件数：{num_files}")
    print(f"训练集数量：{len(train_image_files)}")
    print(f"验证集数量：{len(val_image_files)}")