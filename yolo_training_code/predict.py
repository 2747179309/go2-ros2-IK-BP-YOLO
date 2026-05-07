from ultralytics import YOLO

# 加载你自己训练好的模型
model = YOLO("runs/detect/train/weights/best.pt")

# 对单张图片或文件夹进行预测
results = model.predict(source="path/to/your_test_image.png", save=True, conf=0.5)

# 结果会保存在 runs/detect/predict 文件夹下