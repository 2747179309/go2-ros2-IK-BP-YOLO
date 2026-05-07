import os
import json


def convert_labelme_to_yolo(json_folder, target_label_map):
    # json_folder 指向你的 dataset/labels/val 或 dataset/labels/train
    json_files = [f for f in os.listdir(json_folder) if f.endswith('.json')]

    for json_name in json_files:
        json_path = os.path.join(json_folder, json_name)
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        height = data['imageHeight']
        width = data['imageWidth']

        yolo_lines = []
        for shape in data['shapes']:
            label = shape['label']
            if label not in target_label_map: continue

            cls_id = target_label_map[label]
            points = shape['points']
            # 计算矩形边界
            px = [p[0] for p in points]
            py = [p[1] for p in points]

            dw, dh = 1. / width, 1. / height
            x = (min(px) + max(px)) / 2.0 * dw
            y = (min(py) + max(py)) / 2.0 * dh
            w = (max(px) - min(px)) * dw
            h = (max(py) - min(py)) * dh

            yolo_lines.append(f"{cls_id} {x} {y} {w} {h}")

        # 保存为同名 txt
        txt_path = os.path.join(json_folder, json_name.replace('.json', '.txt'))
        with open(txt_path, 'w') as f:
            f.write('\n'.join(yolo_lines))
    print(f"Finished: {json_folder}")


my_map = {'apple': 0}
convert_labelme_to_yolo(r"D:\大创\dataset\labels\train", my_map)
convert_labelme_to_yolo(r'D:\大创\dataset\labels\val', my_map)