# coding=utf-8
import json
import math
import os
import queue
import threading
import time
import webbrowser
import zipfile
from http.server import HTTPServer, SimpleHTTPRequestHandler

import numpy as np
import requests

API_URL = "http://www.ziroom.com/map/room/list?min_lng=%.6f&max_lng=%.6f&min_lat=%.6f&max_lat=%.6f&p=%d"


class Grid:
    """区块"""

    def __init__(self, lonlat):  # [lon_min,lon_max,lat_min,lat_max]
        self._lon_min = lonlat[0]
        self._lon_max = lonlat[1]
        self._lat_min = lonlat[2]
        self._lat_max = lonlat[3]
        self._page_one_cache = None
        "第一页的缓存"

    def __str__(self):
        return "%.6f,%.6f,%.6f,%.6f" % tuple(self.get_range())

    def get_range(self):
        return [self._lon_min, self._lon_max, self._lat_min, self._lat_max]

    def _json_request(self, lonlat, page_index):
        """联网请求或使用缓存"""
        if page_index == 1 and self._page_one_cache is not None:
            return self._page_one_cache

        url = API_URL % (lonlat[0], lonlat[1], lonlat[2], lonlat[3], page_index)
        retry_time = 0
        while True and retry_time < 10:
            retry_time += 1
            # sys.stdout.write('\r get %s ' % url)
            # sys.stdout.flush()
            try:
                json_str = requests.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_6) AppleWebKit/537.36 (KHTML, "
                                  "like Gecko) Chrome/62.0.3202.94 Safari/537.36",
                    "Referer": "http://www.ziroom.com/map/"
                }, timeout=1).text
                obj = json.loads(json_str)
                if obj["code"] == 200:
                    obj = json.loads(json_str)
                    self._page_one_cache = obj
                    return obj
                else:
                    print("error %s" % json_str)
            except requests.exceptions.ReadTimeout:
                pass
            except Exception as e:
                print(type(e))
                print('error:%s' % e)

    def status(self):
        obj = self._json_request((self._lon_min, self._lon_max, self._lat_min, self._lat_max), 1)
        if len(obj["data"]["rooms"]) == 0:
            return -1
        elif obj["data"]["pages"] == 1:
            # 只有一页，不需要划分
            # 修正，一页仍需要划分，否则数据会有不全
            return -2
        return 0

    def area(self):
        return (self._lon_max - self._lon_min) * 1e5 * (self._lat_max - self._lat_min) * 1e5

    def get_rooms(self, thread_id):
        """获取房间，会获取各页"""
        result = {}
        page_index = 1

        useless_count = 0

        while True:
            last_len = len(result)
            obj = self._json_request((self._lon_min, self._lon_max, self._lat_min, self._lat_max), page_index)

            if obj is None:
                print('线程 %d 结果为空,跳过 %d 页' % (thread_id, page_index))
                page_index += 1
                continue

            for item in obj["data"]["rooms"]:
                result[item["id"]] = item

            pages = obj["data"]["pages"]
            if page_index == pages:
                # 到达最后一页
                print('线程 %d 获取 %d/%d 页完成,获得 %d 处房源,添加 %d 房源' % (
                    thread_id, page_index, pages, len(obj['data']['rooms']), len(result) - last_len))
                return result
            if len(obj["data"]["rooms"]) == 0:
                # 没有数据
                print('线程 %d 获取 %d/%d 页，结果为空' % (thread_id, page_index, pages))
                return result
            if last_len == len(result):
                # 没有产生结果
                useless_count += 1
            if useless_count > 3:
                # 这里不太清原来的逻辑,可能是 result 更新时 id 相同导致相同数据数量没有添加
                print('线程 %d 获取 %d/%d 页，无效数据超限' % (thread_id, page_index, pages))
                return result
            page_index += 1
            print('线程 %d 获取 %d/%d 页完成,获得 %d 房源,添加 %d 房源' % (
                thread_id, page_index, pages, len(obj['data']['rooms']), len(result) - last_len))

    def split(self, count=2):
        """分割"""
        lon_step = (self._lon_max - self._lon_min) / count
        lat_step = (self._lat_max - self._lat_min) / count

        result = []

        for i in range(0, count):
            for j in range(0, count):
                temp = Grid([(self._lon_min + i * lon_step),
                             (self._lon_min + (i + 1) * lon_step),
                             (self._lat_min + j * lat_step),
                             (self._lat_min + (j + 1) * lat_step)])
                result.append(temp)
        return result


class GridManager:
    def __init__(self, lonlat, min_area=1e6, split_count=2, thread_num=4):
        self._q = queue.Queue()
        "队列"

        self._smaller_q = queue.Queue()

        root_grid = Grid(lonlat)
        self._q.put(root_grid)
        self._total_area = root_grid.area()
        self._min_area = min_area
        self._split_count = split_count
        self._thread_num = thread_num
        self._running_thread_num = 0
        self._result = {}
        self._scan_start_time = 0
        self._run_start_time = 0
        self._scanned_area = 0

    def run(self):
        """
        分析这一段算法，出列，如果不为空，则分割，再入列，取下一个
        这样的结果是，当到达第一个最小区域时，整个队列中都是最小区域块，其中一些可能仍没有房源
        """

        self._run_start_time = time.time()
        # 第一步，划分区块

        # 求需要划分多少轮
        # 最大面积除以最小面积，得到倍数，再对 划分数量的平方求对数，取 ceil
        num = int(math.ceil(math.log(self._total_area / self._min_area, self._split_count ** 2)))
        print('划分区块，共需要划分 %d 轮' % num)

        # 划分至最小块
        for i in range(num):
            print('\n划分第 %d/%d 轮，本轮共需划分 %d 个区块' % (i + 1, num, self._q.qsize()))

            # 初始化
            self._scan_start_time = time.time()
            self._smaller_q = queue.Queue()

            # 划分
            self.start_multi_thread(self.split_area)

            # 经过划分，q 已为空，新划分的小块全部位于 _smaller_q 中
            self._q = self._smaller_q

            print('划分第 %d/%d 轮结束,共划分出 %d 个区块\n' % (i + 1, num, self._q.qsize()))

        # 第二步，对划分好的所有最小区块抓取房源
        print('\n开始获取房源,共 %d 个区块' % self._q.qsize())
        self._scan_start_time = time.time()
        self.start_multi_thread(self.get_rooms)
        print("获取房源结束")
        return self._result

    def start_multi_thread(self, target):
        """启动多线程"""
        self._running_thread_num = 0
        threads = []
        thread_num = self._thread_num
        if thread_num > self._q.qsize():
            thread_num = self._q.qsize()
        for i in range(0, thread_num):
            worker = threading.Thread(target=self.work_in_thread, args=(target, i + 1))
            worker.start()
            threads.append(worker)
        # 这里好像不需要 thread 的 join，queue 的 join 也会阻塞，但还是加上线程的 join，以使线程结束后再继续执行
        # 注意调用 task_done()
        # https://docs.python.org/3/library/queue.html
        # http://blog.csdn.net/xiao_huocai/article/details/74781820
        # block until all tasks are done
        self._q.join()
        for t in threads:
            t.join()

    def work_in_thread(self, target, thread_id):
        """在线程中执行,直到出错"""
        while True:
            try:
                self._running_thread_num += 1
                grid = self._q.get(block=True, timeout=1)
                self._scanned_area += 1
                area_index = self._scanned_area
                target(grid, thread_id, area_index)
                # 告知结束一个任务
                self._q.task_done()
            except queue.Empty:
                print('线程 %d 结束，还有 %d 个线程运行中' % (thread_id, self._running_thread_num - 1))
                break
            finally:
                self._running_thread_num -= 1

    def split_area(self, grid, thread_id, area_index):
        """划分区块"""
        status = grid.status()
        if status == -1:
            # 为空
            self.print_progress('结果为空，移除', thread_id, area_index)
        else:
            # 原本想一页的数据直接加进队列，后来发现数据不全，仍需要划分
            # 需要划分
            self.print_progress('结果不为空，进行划分', thread_id, area_index)
            for item in grid.split(count=self._split_count):
                self._smaller_q.put(item)

    def get_rooms(self, grid, thread_id, area_index):
        """获取房源"""
        self._result.update(grid.get_rooms(thread_id))
        self.print_progress('当前共 %d 个房源' % (len(self._result)), thread_id, area_index)

    def print_progress(self, text, thread_id, area_index):
        """输出进度"""
        spend_time = time.time() - self._scan_start_time
        # 剩余时间，包括运行中的线程
        remain_time = (time.time() - self._scan_start_time) / self._scanned_area * (
                self._q.qsize() + self._running_thread_num)
        all_spend_time = time.time() - self._run_start_time
        text = '线程 %d:区块 %d，%s，剩余区块 %d，任务花费时间 %ds，预计剩余 %ds，总花费时间 %ds' \
               % (thread_id, area_index, text, self._q.qsize(), spend_time, remain_time, all_spend_time)
        print(text)


class Action:
    def __init__(self, grid_range=None, port=2233, thread_num=64):
        """

        :param grid_range: 经纬度范围，参数格式[lon_min,lon_max,lat_min,lat_max]，默认为北京
        :param thread_num: 线程数
        """
        self.grid_range = grid_range
        if self.grid_range is None:
            self.grid_range = [115.7, 117.4, 39.4, 41.6]  # 北京市范围，
        self.port = port
        self.thread_num = thread_num

    def main(self):
        prompt = """
请选择操作
0-退出
1-爬取
2-启动 web 服务并打开浏览器
3-打开浏览器
4-分析均价
5-比较价格
"""
        choice = int(input(prompt))
        if choice == 0:
            exit()
        elif choice == 1:
            self.crawl()
        elif choice == 2:
            self.start_web_server(os.path.abspath('web'), self.port, True)
        elif choice == 3:
            self.open_in_browser(self.port)
        elif choice == 4:
            self.analyze_rooms('rooms')
        elif choice == 5:
            self.compare_rooms('rooms/all_rooms-2018-02-27-115445.zip', 'rooms/all_rooms-2018-08-21-103518.zip')

    def crawl(self):
        """爬取"""
        # 用于测试
        # gm = GridManager(grid_range, min_area=1e9)
        gm = GridManager(self.grid_range, thread_num=self.thread_num)
        """
        一开始认为受核心、GIL 以及服务器限制，增加线程数可能影响不大。
        后来发现可能是 IO 密集型关系，增加线程数有用（https://www.zhihu.com/question/23474039），而且服务器也未作限制。
        python 多线程这一块原理不是很了解

        最后爬取 2000 个区块，13000 房源
        30 线程耗时 74s
        60 线程耗时 58s
        100 线程耗时有过 41s，不过有时候就会超时了。
        """
        all_rooms = gm.run()
        available_rooms = list(
            filter(lambda x: x["room_status"] != "ycz" and x["room_status"] != "yxd", all_rooms.values()))
        share_rooms = list(filter(lambda x: x["is_whole"] == 0, available_rooms))
        whole_rooms = list(filter(lambda x: x["is_whole"] == 1, available_rooms))

        print("房源 %d,可租 %d,整租 %d,合租 %d" % (len(all_rooms), len(available_rooms), len(whole_rooms), len(share_rooms)))

        date_str = time.strftime("%Y-%m-%d-%H%M%S", time.localtime())
        with zipfile.ZipFile('rooms/all_rooms-%s.zip' % date_str, 'w', zipfile.ZIP_DEFLATED) as f:
            f.writestr('all_rooms.json', json.dumps(all_rooms))
        with zipfile.ZipFile('web/share_rooms.zip', 'w', zipfile.ZIP_DEFLATED) as f:
            f.writestr('share_rooms.json', json.dumps(share_rooms))
        with zipfile.ZipFile('web/whole_rooms.zip', 'w', zipfile.ZIP_DEFLATED) as f:
            f.writestr('whole_rooms.json', json.dumps(whole_rooms))
        print('保存结果完成')

    def start_web_server(self, web_dir, port, open_url=False):
        """启动 web 服务器"""
        os.chdir(web_dir)
        print('starting server, port', port)
        server_address = ('', port)
        httpd = HTTPServer(server_address, SimpleHTTPRequestHandler)
        print('running server...')
        if open_url:
            self.open_in_browser(port)
        httpd.serve_forever()

    @staticmethod
    def open_in_browser(port):
        """打开"""
        url = f'http://localhost:{port}'
        webbrowser.open_new_tab(url)
        time.sleep(0.5)

    def analyze_rooms(self, dir_path):
        """分析房源"""
        files = os.listdir(dir_path)
        last_avg_price = None
        for file in files:
            if file.endswith('.zip'):
                path = os.path.join(dir_path, file)
                avg_price = self.analyze_file(path)
                if last_avg_price:
                    print()
                    print(f'合租平均上涨 {avg_price[0]-last_avg_price[0]:#.2f}')
                    print(f'整租平均上涨 {avg_price[1]-last_avg_price[1]:#.2f}')
                last_avg_price = avg_price

    def analyze_file(self, file_path):
        """分析文件"""
        print(f'\n分析均价 {file_path}')
        print(f'爬取日期 {self.get_crawl_date(file_path)}')
        rooms = self.load_rooms(file_path)
        if rooms:
            available_rooms = list(
                filter(lambda x: x["room_status"] != "ycz" and x["room_status"] != "yxd", rooms.values()))
            share_rooms = list(filter(lambda x: x["is_whole"] == 0, available_rooms))
            whole_rooms = list(filter(lambda x: x["is_whole"] == 1, available_rooms))

            print(f'共 {len(available_rooms)} 处房源')
            count, avg_price1, avg_area = self.calculate_average_price(share_rooms)
            print(f'合租有效房源 {count},均价 {avg_price1:#.2f},均面积 {avg_area:#.2f}')

            count, avg_price2, avg_area = self.calculate_average_price(whole_rooms)
            print(f'整租有效房源 {count},均价 {avg_price2:#.2f},均面积 {avg_area:#.2f}')
            return avg_price1, avg_price2

    @staticmethod
    def get_crawl_date(file_path):
        """截取爬取日期"""
        file_name = os.path.split(file_path)[1]
        file_name = os.path.splitext(file_name)[0]
        # 去前后再重新拼接
        crawl_date = '-'.join(file_name.split('-')[1:-1])
        return crawl_date

    def calculate_average_price(self, rooms):
        """计算均价"""
        all_price = 0
        all_area = 0
        count = 0
        for room in rooms:
            price = self.get_room_price(room)
            if price <= 0 or room['usage_area'] <= 0:
                # 为 0 过滤
                # print(room)
                continue
            count += 1
            all_price += price
            all_area += room['usage_area']
            # print(f"价格 {price} 面积 {room['usage_area']}")
        return count, all_price / count, all_area / count

    @staticmethod
    def load_rooms(file_path) -> dict:
        with zipfile.ZipFile(file_path) as zip_file:
            json_file_path = 'all_rooms.json'
            if json_file_path in zip_file.namelist():
                obj = json.loads(zip_file.read(json_file_path).decode())
                return obj
        return {}

    def compare_rooms(self, path1, path2):
        """比较房源"""
        print(f'比较价格 {self.get_crawl_date(path1)} → {self.get_crawl_date(path2)}')
        rooms1 = self.load_rooms(path1)
        rooms2 = self.load_rooms(path2)

        # 相同房源
        same_share_rooms = []
        same_whole_rooms = []
        if rooms1 and rooms2:
            for k1, v1 in rooms1.items():
                if k1 in rooms2.keys():
                    v2 = rooms2[k1]
                    price1 = self.get_room_price(v2)
                    price2 = self.get_room_price(v1)
                    if price1 and price2:
                        # 有一个为 0 则过滤
                        delta_price = price1 - price2
                        # print(f"id 为 {k1} 的房源,价格 {self.get_room_price(v1)} → {self.get_room_price(v2)},{delta_price}")
                        v2['delta_price'] = delta_price
                        if v2['is_whole'] == 0:
                            same_share_rooms.append(v2)
                        else:
                            same_whole_rooms.append(v2)
        print('分析合租')
        self.analyze_price(same_share_rooms)
        print('\n分析整租')
        self.analyze_price(same_whole_rooms)

    def analyze_price(self, rooms):
        """比较价格"""
        increase_rooms = list(filter(lambda x: x['delta_price'] > 0, rooms))
        decrease_rooms = list(filter(lambda x: x['delta_price'] < 0, rooms))
        same_rooms = list(filter(lambda x: x['delta_price'] == 0, rooms))
        print(f'共有 {len(rooms)} 处房源相同,其中上涨 {len(increase_rooms)} 处,下降 {len(decrease_rooms)} 处,持平 {len(same_rooms)} 处')

        average_increase_price = np.average([room['delta_price'] for room in rooms])
        print(f"在全部 {len(rooms)} 处房源中,平均涨价 {average_increase_price:#.2f},")

        max_increase_price_room = increase_rooms[0]
        for room in increase_rooms:
            delta_price = room['delta_price']
            if delta_price > max_increase_price_room['delta_price']:
                max_increase_price_room = room
        average_increase_price = np.average([room['delta_price'] for room in increase_rooms])
        print(f"在 {len(increase_rooms)} 处上涨的房源中,平均涨价 {average_increase_price:#.2f},"
              f"最高上涨 {max_increase_price_room['delta_price']:#.2f},"
              f"当前价 {self.get_room_price(max_increase_price_room)},"
              f"围观地址为 http://www.ziroom.com/z/vr/{max_increase_price_room['id']}.html")

    @staticmethod
    def get_room_price(room):
        """获取价格"""
        price_duanzu = room['sell_price_duanzu']
        price_day = room['sell_price_day']
        if price_day > 0:
            # 按天的过滤
            return 0
        elif price_duanzu:
            return price_duanzu
        else:
            return room['sell_price']


if __name__ == '__main__':
    Action(grid_range=[115.7, 117.4, 39.4, 41.6]).main()
