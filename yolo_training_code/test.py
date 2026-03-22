from ultralytics import YOLO
model = YOLO('runs/detect/train/weights/bestn.pt')
model.val(data='data.yaml')  # 计算指标如mAP
model.predict(source='dataset/dataset/test', save=True)  # 测试图像路径
