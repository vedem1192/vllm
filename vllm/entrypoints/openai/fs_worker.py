import threading
import time
import requests
from vllm.logger import init_logger
logger = init_logger('vllm.entrypoints.openai.heart_beat')

def heart_beat_worker(obj):
    while True:
        time.sleep(45)
        obj.send_heart_beat()

class FsWorker:
    def __init__(self, controller, served_model_name, worder_address):
        self.controller_addr = controller
        self.heart_beat_thread = None
        self.model_name = served_model_name
        self.worker_addr = worder_address

    def register_to_controller(self):
        logger.info(":: Registering with controller")

        url = self.controller_addr + "/register_worker"
        data = {
            "worker_name": self.worker_addr,
            "check_heart_beat": True,
            "worker_status": {
                "model_names": self.model_name,
                "speed": 1,
                "queue_length": 0
            }
        }

        logger.info(f"URL : {url}")
        logger.info(data)

        r = requests.post(url, json=data)
        assert r.status_code == 200


    def send_heart_beat(self):
        logger.info(
            f"Send heart beat. Models: {self.model_name}"
            f"Semaphore:  NA"
            f"call_ct:  NA"
            f"worker_id:  NA"
        )

        url = self.controller_addr + "/receive_heart_beat"

        while True:
            try:
                ret = requests.post(
                    url,
                    json={
                        "worker_name": self.worker_addr,
                        # "queue_length": self.get_queue_length(),
                        "queue_length": 0,
                    },
                    timeout=5,
                )
                exist = ret.json()["exist"]
                break
            except (requests.exceptions.RequestException, KeyError) as e:
                logger.error(f"heart beat error: {e}")
            time.sleep(5)

        if not exist:
            self.register_to_controller()

    def init_heart_beat(self):
            self.register_to_controller()
            self.heart_beat_thread = threading.Thread(
                target=heart_beat_worker,
                args=(self,),
                daemon=True,
            )
            self.heart_beat_thread.start()