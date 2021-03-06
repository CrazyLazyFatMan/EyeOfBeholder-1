from channels.generic.websocket import WebsocketConsumer, AsyncWebsocketConsumer
import json
import asyncio
from channels.layers import get_channel_layer, ChannelFull
from string import ascii_letters
import random
import operator
import time
import copy
import collections


face_channel_layer = get_channel_layer("face")
coin_channel_layer = get_channel_layer("coin")
clock_channel_layer = get_channel_layer("clock")


class StreamConsumer(AsyncWebsocketConsumer):
    groups = ["recognize-faces", "recognize-coins"]

    def __init__(self, *args):
        super().__init__(*args)
        self.uid = "".join(random.choice(ascii_letters) for _ in range(8))
        self.rec_coins = {}
        self.cnt_rec_coins = 0
        self.coins_queue = []
        print("consumer created", flush=True)
        self.response_min_cnt = 4
        self.coin_info = {}
        self.connected = False

    async def sync_clock(self):
        while self.connected:
            message = {"type": "sync_clock", "timestamp": time.time(), "uid": self.uid}
            try:
                await asyncio.gather(
                    clock_channel_layer.send("recognizefaces", message),
                    clock_channel_layer.send("recognizecoins", message),
                    self.send(json.dumps(message)),
                )
                print("sync clock", flush=True)
            except ChannelFull:
                print(f'sync clock exception: channel full')
            await asyncio.sleep(4)

    async def connect(self):
        print('Connection successful!')
        await self.accept()
        self.connected = True
        asyncio.get_event_loop().create_task(self.sync_clock())

    async def faces_ready(self, message):
        if message["uid"] == self.uid:
            res = copy.deepcopy(message)
            res['type'] = 'face'
            await self.send(json.dumps(res))

    def extend_by_featured(self, coins, response):
        try:
            for coin_descr in response:
                s = set(coin["id"] for coin in coins)
                if len(s) >= self.response_min_cnt:
                    return
                if coin_descr.get("featured", False) and coin_descr["id"] not in s:
                    coins.append(coin_descr)
        except:
            pass

    async def coins_ready(self, message):
        if message["uid"] == self.uid:
            res = copy.deepcopy(message)
            res['type'] = 'coin'
            print(f'{self.uid}: coins ready')
            for coin_descr in message["text"]:
                coin_id = coin_descr["id"]
                self.coin_info[coin_id] = coin_descr
                if not coin_descr.get("featured", False):
                    self.coins_queue.append((time.time(), coin_id))
            now, window = time.time(), 5
            self.coins_queue = [(t, coin_id) for t, coin_id in self.coins_queue if now - t <= window]
            cnt = collections.Counter(coin_id for t, coin_id in self.coins_queue)
            coins = [self.coin_info[coin_id] for coin_id, counts in cnt.items()]
            self.extend_by_featured(coins, response=message["text"])
            resp = {
                "type": "recognized_coins",
                "text": coins
            }
            await asyncio.gather(
                self.send(json.dumps(resp)),
                self.send(json.dumps(res)),
            )

    async def coins_ready_old(self, message):
        if message["uid"] == self.uid:
            res = copy.deepcopy(message)
            res['type'] = 'coin'
            print(f'{self.uid}: coins ready')

            for coin in message["text"]:
                self.cnt_rec_coins += 1
                self.rec_coins[coin[4]] = 1 + self.rec_coins.get(coin[4], 0)

            if self.cnt_rec_coins >= 20:
                coins = [(key, value) for key, value in self.rec_coins.items()]
                coins = list(reversed(sorted(coins, key=operator.itemgetter(1))))
                resp = {
                    "type": "recognized_coins",
                    "text": coins
                }
                await self.send(json.dumps(resp))
                self.cnt_rec_coins = 0
                self.rec_coins = {}

            await self.send(json.dumps(res))

    async def receive(self, text_data=None, bytes_data=None):
        try:
            print(f"{self.uid}: receive {len(text_data) if text_data else 0} text data, {len(bytes_data) if bytes_data else 0} bytes data")
            if text_data:
                await asyncio.gather(
                    face_channel_layer.send("recognizefaces",
                                            {"type": "set_language", "lang": text_data, "uid": self.uid}),
                    coin_channel_layer.send("recognizecoins",
                                            {"type": "set_language", "lang": text_data, "uid": self.uid}),
                )
            else:
                await asyncio.gather(
                    face_channel_layer.send("recognizefaces",
                                            {"type": "recognize", "bytes_data": bytes_data, "uid": self.uid}),
                    coin_channel_layer.send("recognizecoins",
                                            {"type": "recognize", "bytes_data": bytes_data, "uid": self.uid}),
                )
        except ChannelFull:
            print(f"{self.uid}: channe lfull")
        except Exception as e:
            print(f"{self.uid}: unknown exception: {e}: {type(e)}")

    async def disconnect(self, close_code):
        self.connected = False
        print("disconnect ", close_code, flush=True)
