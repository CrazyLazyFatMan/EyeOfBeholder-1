import os
from io import BytesIO
import time
import numpy as np
import sys
import copy
from PIL import Image
from channels.consumer import SyncConsumer
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
import cv2
from FRS.models import DialogUser
from datetime import datetime
import base64
from vef.settings import SERVANT_DIR
from string import ascii_letters
import random
import pathlib as pl
from django.db import connection

server_channel_layer = get_channel_layer("server")

urfolder = str(pl.Path(__file__).parents[1])


def unknown():
    unk = 0
    for f in os.listdir(urfolder + '\\FRS\\static\\facephotos'):
        unk += 1
    return unk


def area(box):
    return abs(box[2] - box[0]) * abs(box[3] - box[1])


def sorted_faces(faces, boxes, n=5):
    idxs = np.array([i for (b, i) in sorted([(area(b), i) for i, b in enumerate(boxes)], reverse=True)[:n]])
    return np.array(faces)[idxs], np.array(boxes)[idxs]


def get_image_data_from_bytes_data(bytes_data):
    image_bytes_data = bytes_data[13:]
    image_bytes_data = BytesIO(image_bytes_data)
    img = Image.open(image_bytes_data)
    img_data = np.array(img)
    timestamp = float(bytes_data[:13]) / 1000
    return timestamp, img_data


class TimeShifter:
    def get_age(self, timestamp, uid=None):
        if not hasattr(self, "shift"):
            self.set_shift(timestamp, uid)
        return time.time() - timestamp + self.shift

    def set_shift(self, timestamp, uid=None):
        now = time.time()
        self.shift = now - timestamp
        print(f"{uid or '?'} sync clock, shift = {self.shift} seconds")

    def sync_clock(self, message):
        try:
            timestamp = message["timestamp"]
            self.set_shift(timestamp, message["uid"])
        except Exception as e:
            print(e)


class SqliteDialoguser:
    UNKNOWN = "Unknown"

    def __init__(self):
        self.type = "sqlite3"
        self.dialog_uids = set(user.uid for user in DialogUser.objects.all())
        self.cached_vectors = {}

    def __iter__(self):
        """итерация for возвращает dialog_uid"""
        return iter(copy.deepcopy(self.dialog_uids))

    def get(self, dialog_uid):
        """Возвращает embed вектор юзера"""
        if dialog_uid not in self.cached_vectors:
            self.cached_vectors[dialog_uid] = np.frombuffer(DialogUser.objects.get(uid=dialog_uid).vector, dtype=np.float32)
        return self.cached_vectors[dialog_uid]

    def checkOutgoingName(self, dialog_uid):
        if dialog_uid == self.UNKNOWN:
            return dialog_uid
        self.add_dialog_uid(dialog_uid)
        return dialog_uid

    @staticmethod
    def randomString(length=10, pool=ascii_letters):
        return "".join(random.choice(pool) for _ in range(length))

    def _get_all_uids(self):
        return self.dialog_uids

    def recache_all_uids(self):
        self.dialog_uids = set(user.uid for user in DialogUser.objects.all())

    def add_dialog_uid(self, dialog_uid):
        self.dialog_uids.add(dialog_uid)



class FaceRecognitionConsumer(SyncConsumer, TimeShifter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        print("creating face worker...", flush=True)

        sys.path.append(SERVANT_DIR)

        from FaceRecognition.InsightFaceRecognition import FaceRecognizer, RecognizerConfig
        from FaceDetection.RetinaFaceDetector import RetinaFace
        from FaceDetection.Config import DetectorConfig

        self.dataBase = SqliteDialoguser()

        sys.path.pop()

        #os.environ.setdefault("MXNET_CUDNN_AUTOTUNE_DEFAULT", "1")
        print("preparing detector...")
        self.detector = RetinaFace(
            prefix=DetectorConfig.PREFIX,
            epoch=DetectorConfig.EPOCH
        )
        print("detector is ready")
        print("preparing recognizer...")
        self.recognizer = FaceRecognizer(
            prefix=RecognizerConfig.PREFIX,
            epoch=RecognizerConfig.EPOCH,
            dataBase=self.dataBase,
            detector=self.detector
        )
        print("recognizer is ready")
        self.fps_counter = 0
        self.fps_start = time.time()
        self.actual_fps = 0
        self.language = {}
        self.last_filtered = 0
        print("face worker created", flush=True)

    def filter_users(self):
        try:
            DialogUser.objects.filter(name="").delete()
        except:
            pass
        print("users filtered", flush=True)


    def recognize(self, message):
        try:
            if time.time() - self.last_filtered > 5*60:
                self.filter_users()
                self.last_filtered = time.time()
            uid = message["uid"]
            timestamp, img_data = get_image_data_from_bytes_data(message["bytes_data"])
            age = self.get_age(timestamp, uid)
            print(f"face {'pass: ' if age >= 1 else 'go: '} {age}")
            if age >= 1:
                return
            start_recog = time.time()
            faces, boxes, landmarks = self.recognizer.detectFaces(img_data)
            current_user_uid, photo_slice, photo_slice_b64 = [None]*3
            if len(boxes) > 0:
                # y1 x1 y2 x2
                faces, boxes = sorted_faces(faces, boxes, 10)
                embeddings = self.recognizer._getEmbedding(faces)
                y1, x1, y2, x2 = boxes[0]
                w, h, div, (maxy, maxx, *_) = x2 - x1, y2 - y1, 5, img_data.shape
                photo_slice = img_data[
                    max(y1 - h // div, 0):min(y2 + h // div, maxy - 1),
                    max(x1 - w // div, 0):min(x2 + w // div, maxx - 1)
                ]
                users = []
                for i, embed in enumerate(embeddings):
                    result, scores = self.recognizer.identify(embed)
                    # Самое большое лицо
                    if i == 0:
                        # Если мы его знаем
                        tm = str(datetime.now())[:16]
                        if result != SqliteDialoguser.UNKNOWN:
                            cursor = connection.cursor()
                            cursor.execute(
                                f"UPDATE main.FRS_dialoguser SET time_enrolled = '{datetime.now()}' WHERE uid = '{result}'")
                            connection.close()
                            try:
                                print(f"Известный персонаж: {result}")
                                visits = open(
                                    urfolder + '\\FRS\\static\\facephotos\\' + result + '\\' + result + '.txt',
                                    'r')
                                all_visits = visits.read()
                                if tm not in all_visits:
                                    visits_append = open(
                                        urfolder + '\\FRS\\static\\facephotos\\' + result + '\\' + result + '.txt',
                                        'a')
                                    print(str(tm), end='\n', file=visits_append)
                                    visits_append.close()
                                visits.close()
                            except:
                                result = SqliteDialoguser.randomString()
                                print(f"Это новый персонаж: {result}")
                                unk = unknown()
                                os.makedirs(urfolder + '\\FRS\\static\\facephotos\\' + result)
                                os.chdir(urfolder + '\\FRS\\static\\facephotos\\' + result)
                                photo_slice = cv2.cvtColor(photo_slice, cv2.COLOR_BGR2RGB)
                                cv2.imwrite(f"photo_{result.replace('/', ' ')}.png", photo_slice)
                                user = DialogUser(
                                    uid=result,
                                    time_enrolled=datetime.now(),
                                    photo=photo_slice.tobytes(),
                                    name='Незнакомец №' + str(unk),
                                    vector=embed.tobytes(),
                                )
                                user.save()
                                visits = open(
                                    urfolder + '\\FRS\\static\\facephotos\\' + result + '\\' + result + '.txt',
                                    'a')
                                print(str(tm), end='\n', file=visits)
                                visits.close()
                                self.dataBase.add_dialog_uid(result)
                        else:
                            result = SqliteDialoguser.randomString()
                            print(f"Это новый персонаж: {result}")
                            unk = unknown()
                            os.makedirs(urfolder + '\\FRS\\static\\facephotos\\' + result)
                            os.chdir(urfolder + '\\FRS\\static\\facephotos\\' + result)
                            photo_slice = cv2.cvtColor(photo_slice, cv2.COLOR_BGR2RGB)
                            cv2.imwrite(f"photo_{result.replace('/', ' ')}.png", photo_slice)
                            user = DialogUser(
                                uid=result,
                                time_enrolled=datetime.now(),
                                photo=photo_slice.tobytes(),
                                name='Незнакомец №' + str(unk),
                                vector=embed.tobytes(),
                            )
                            user.save()
                            visits = open(
                                urfolder + '\\FRS\\static\\facephotos\\' + result + '\\' + result + '.txt', 'a')
                            print(str(tm), end='\n', file=visits)
                            visits.close()
                            self.dataBase.add_dialog_uid(result)
                        os.chdir(urfolder)
                        current_user_uid = result or None
                    display_name = DialogUser.objects.get(uid=result).name if result != SqliteDialoguser.UNKNOWN else "try again"
                    users.append(display_name)
            boxes = boxes.tolist()
            response = [b + [users[idx]] for idx, b in enumerate(boxes)]
            if photo_slice is not None:
                photo_slice_b64 = cv2.cvtColor(photo_slice, cv2.COLOR_BGR2RGB)
                photo_slice_b64 = base64.b64encode(cv2.imencode('.png', photo_slice_b64)[1]).decode()
            end_recog = time.time()
            print(f"recog time = {end_recog - start_recog}")

            async_to_sync(server_channel_layer.group_send)(
                "recognize-faces",
                {
                    "type": "faces_ready",
                    "text": response,
                    "uid": uid,
                },
            )

            display_name = DialogUser.objects.get(uid=current_user_uid).name if current_user_uid else ""
            async_to_sync(server_channel_layer.group_send)(
                "dialog-recognize-faces",
                {
                    "type": "dialog_faces_ready",
                    "uid": uid,
                    "dialog_uid": current_user_uid,
                    "dialog_photo": photo_slice_b64,
                    "display_name": display_name
                },
            )
        except KeyboardInterrupt as e:
            raise e
        except Exception as e:
            self.dataBase.recache_all_uids()
            print("uids recached", flush=True)
            print(e)
            raise e

    def register(self):
        pass

    def set_language(self, message):
        try:
            lang = message["lang"]
            uid = message["uid"]
            self.language[uid] = lang
        except Exception as e:
            print(e)

dnn = None
