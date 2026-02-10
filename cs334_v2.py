from transformers import BlipProcessor, BlipForConditionalGeneration
import torch
from PIL import Image
import cv2
from reachy_mini import ReachyMini
import time

# Load BLIP model
processor = BlipProcessor.from_pretrained("Salesforce/blip-image-captioning-base")
model = BlipForConditionalGeneration.from_pretrained("Salesforce/blip-image-captioning-base")
device = "cuda" if torch.cuda.is_available() else "cpu"
model.to(device)

INFERENCE_INTERVAL = 2.0
last_inference = 0
backend = "default"

with ReachyMini(media_backend=backend) as reachy:
    print("Reachy live caption running. Press Ctrl+C to quit.")

    while True:
        frame = reachy.media.get_frame()
        if frame is None:
            continue

        cv2.imshow("Reachy Mini Camera", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

        now = time.time()
        if now - last_inference > INFERENCE_INTERVAL:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(frame_rgb)

            # Preprocess and move to device
            inputs = processor(images=image, return_tensors="pt").to(device)

            # Generate caption
            with torch.no_grad():
                output_ids = model.generate(**inputs, max_new_tokens=50)

            caption = processor.batch_decode(output_ids, skip_special_tokens=True)[0]
            print("Caption:", caption)

            last_inference = now

cv2.destroyAllWindows()