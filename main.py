# main.py 主逻辑：包括字段拼接、模拟请求
import json
import os
import time
import random
import logging
import hashlib
import traceback
import requests
import urllib.parse
from push import push
from log_utils import setup_logging
from config import data, headers, cookies, READ_NUM, PUSH_METHOD, book, chapter


# 加密盐及其它默认值
KEY = "3c5c8717f3daf09iop3423zafeqoi"
READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
FIX_SYNCKEY_URL = "https://weread.qq.com/web/book/chapterInfos"
COOKIE_DATA_VARIANTS = [{"rq": "%2Fweb%2Fbook%2Fread", "ql": False},{"rq": "%2Fweb%2Fbook%2Fread", "ql": True},{"rq": "%2Fweb%2Fbook%2Fread"},]


def encode_data(data):
    """数据编码"""
    return '&'.join(f"{k}={urllib.parse.quote(str(data[k]), safe='')}" for k in sorted(data.keys()))


def cal_hash(input_string):
    """计算哈希值"""
    _7032f5 = 0x15051505
    _cc1055 = _7032f5
    length = len(input_string)
    _19094e = length - 1

    while _19094e > 0:
        _7032f5 = 0x7fffffff & (_7032f5 ^ ord(input_string[_19094e]) << (length - _19094e) % 30)
        _cc1055 = 0x7fffffff & (_cc1055 ^ ord(input_string[_19094e - 1]) << _19094e % 30)
        _19094e -= 2

    return hex(_7032f5 + _cc1055)[2:].lower()

def get_wr_skey():
    """刷新cookie密钥"""
    for cookie_data in COOKIE_DATA_VARIANTS:
        try:
            response = requests.post(RENEW_URL,headers=headers,cookies=cookies,data=json.dumps(cookie_data, separators=(',', ':')),timeout=10)
            
            if 'wr_skey' in response.cookies:
                return response.cookies['wr_skey'][:8]
            else:
                continue
        except requests.RequestException as exc:
            logging.warning(f"refresh_cookie 请求失败，payload={cookie_data}，原因：{exc}")
            continue
        
        
    return None

def fix_no_synckey():
    requests.post(FIX_SYNCKEY_URL, headers=headers, cookies=cookies,data=json.dumps({"bookIds":["3300060341"]}, separators=(',', ':')))

refresh_print = setup_logging()

def refresh_cookie(silent=False):
    """刷新 cookie

    silent=True 时不推送、不抛异常，仅返回布尔值（用于顶层 safe_push 之外的场景）。
    """
    logging.info("刷新 cookie")
    new_skey = get_wr_skey()
    if new_skey:
        cookies['wr_skey'] = new_skey
        logging.info(f"密钥刷新成功，新密钥：{new_skey[:2]}***")
        logging.info("重新本次阅读。")
        return True

    ERROR_CODE = "无法获取新密钥或者 WXREAD_CURL_BASH 配置有误，终止运行。"
    logging.error(ERROR_CODE)
    if silent:
        return False
    push(ERROR_CODE, PUSH_METHOD, is_success=False)
    raise Exception(ERROR_CODE)


def safe_push(content, is_success):
    """推送包装：推送自身失败也不能影响主流程"""
    try:
        if PUSH_METHOD in (None, ''):
            logging.warning("未配置推送渠道，跳过推送。")
            return False
        return push(content, PUSH_METHOD, is_success=is_success)
    except Exception as push_exc:
        logging.error("推送失败（不影响任务本身）：%s", push_exc)
        return False


# 连续失败的最大容忍次数，超过则终止，防止死循环烧额度
MAX_FAIL_STREAK = int(os.getenv('MAX_FAIL_STREAK') or 10)


def run_read():
    """执行阅读主流程，返回实际完成的次数"""
    index = 1
    lastTime = int(time.time()) - 30
    fail_streak = 0
    logging.info(f"一共需要阅读 {READ_NUM} 次。")

    while index <= READ_NUM:
        data.pop('s')
        data['b'] = random.choice(book)
        data['c'] = random.choice(chapter)
        thisTime = int(time.time())
        data['ct'] = thisTime
        data['rt'] = thisTime - lastTime
        data['ts'] = int(thisTime * 1000) + random.randint(0, 1000)
        data['rn'] = random.randint(0, 1000)
        data['sg'] = hashlib.sha256(f"{data['ts']}{data['rn']}{KEY}".encode()).hexdigest()
        data['s'] = cal_hash(encode_data(data))

        refresh_print(f"阅读进度: 第 {index}/{READ_NUM} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
        logging.debug("data: %s", data)
        response = requests.post(READ_URL, headers=headers, cookies=cookies,
                                 data=json.dumps(data, separators=(',', ':')), timeout=15)
        resData = response.json()
        logging.debug("response: %s", resData)

        if 'succ' in resData:
            fail_streak = 0
            if 'synckey' in resData:
                lastTime = thisTime
                index += 1
                time.sleep(30)
                refresh_print(f"阅读进度: 第 {min(index, READ_NUM + 1) - 1}/{READ_NUM} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
            else:
                logging.warning("无 synckey，尝试修复...")
                fix_no_synckey()
        else:
            fail_streak += 1
            logging.warning("cookie 已过期，尝试刷新...（连续失败 %d/%d 次）", fail_streak, MAX_FAIL_STREAK)
            if fail_streak >= MAX_FAIL_STREAK:
                # 连续失败达上限，主动终止，避免无限循环烧掉 Actions 额度
                raise Exception(
                    f"连续 {MAX_FAIL_STREAK} 次请求失败（cookie 刷新后仍无效），"
                    f"已终止任务。请检查 WXREAD_CURL_BASH 是否已过期。"
                )
            # 刷新失败会抛异常，交由 main() 统一捕获并推送
            refresh_cookie()
            # 刷新后稍作等待，避免高频请求触发风控
            time.sleep(5)

    return index - 1


def main():
    """顶层入口：捕获所有异常，任何失败场景都推送通知"""
    setup_logging()
    logging.info("=" * 50)
    logging.info("微信读书自动阅读任务开始")
    logging.info("=" * 50)

    try:
        # 首次刷新 cookie 也放进 try，保证失败也能被统一捕获并推送
        refresh_cookie()
        done = run_read()
    except Exception as exc:
        # 任何异常：记录完整堆栈 + 推送失败通知
        tb = traceback.format_exc()
        logging.error("任务发生异常：%s", exc)
        logging.error("完整堆栈：\n%s", tb)
        safe_push(
            f"任务【失败】\n"
            f"错误类型：{type(exc).__name__}\n"
            f"错误信息：{exc}\n\n"
            f"堆栈摘要（末尾 500 字）：\n{tb[-500:]}",
            is_success=False,
        )
        return False

    logging.info("阅读脚本已完成。")
    safe_push(f"微信读书自动阅读完成。\n阅读时长：{done * 0.5} 分钟。", is_success=True)
    return True


if __name__ == '__main__':
    try:
        ok = main()
        if not ok:
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception:
        # 兜底：main 内部理论上已推送，这里只保证 Actions 标红
        logging.exception("任务最终失败（未被 main 捕获）")
        try:
            push(f"任务发生未捕获异常，请查看 Actions 日志。", PUSH_METHOD, is_success=False)
        except Exception:
            pass
        raise SystemExit(1)
