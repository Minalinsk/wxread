import json
import logging
import os
import random
import time

import requests

from config import (
    PUSHPLUS_TOKEN,
    SERVERCHAN_SPT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WXPUSHER_SPT,
    WEWORK_WEBHOOK,
    WEWORK_MSG_TYPE,
)

logger = logging.getLogger(__name__)


def _check_code(response, ok_codes, what):
    """HTTP 200 不等于"推送成功"。

    pushplus / wxpusher / serverchan 在 token 写错、套餐过期、内容被拦的时候
    也是回 HTTP 200，只是 body 里的 code 变了。原来这三个渠道只做了
    `raise_for_status()` 就直接 `return True`，于是"其实没推出去"会被记成成功，
    任务日志一片祥和、人是收不到消息的。

    返回 (是否成功, 说明)。body 不是 JSON、或者没有 code 字段时，按"判断不了"
    处理 —— 保持旧行为（当成功）并记一条 debug，免得因为认不出新格式而误报失败。
    """
    try:
        res = response.json()
    except ValueError:
        logger.debug("%s 的响应不是 JSON，按成功处理：%s", what, (response.text or "")[:200])
        return True, "非 JSON 响应"
    if not isinstance(res, dict) or "code" not in res:
        logger.debug("%s 的响应里没有 code，按成功处理：%s", what, (response.text or "")[:200])
        return True, "响应里没有 code"
    if res.get("code") in ok_codes:
        return True, "ok"
    return False, "code=%s msg=%s" % (
        res.get("code"),
        res.get("msg") or res.get("message") or res.get("errmsg") or "",
    )


class PushNotification:
    def __init__(self):
        self.pushplus_url = "https://www.pushplus.plus/send"
        self.telegram_url = "https://api.telegram.org/bot{}/sendMessage"
        self.server_chan_url = "https://sctapi.ftqq.com/{}.send"
        self.wxpusher_simple_url = "https://wxpusher.zjiecode.com/api/send/message/{}/{}"
        self.headers = {"Content-Type": "application/json"}
        self.proxies = {
            "http": os.getenv("http_proxy"),
            "https": os.getenv("https_proxy"),
        }

    def push_pushplus(self, content, token, is_success):
        attempts = 5
        title = f"微信阅读-{'成功' if is_success else '失败'}"
        for attempt in range(attempts):
            try:
                response = requests.post(
                    self.pushplus_url,
                    data=json.dumps({"token": token, "title": title,"content": content,}).encode("utf-8"),headers=self.headers,timeout=10,)
                response.raise_for_status()
                logger.info("PushPlus 响应: %s", response.text)
                ok, why = _check_code(response, (200, "200"), "PushPlus")
                if ok:
                    return True
                # token 错 / 套餐过期这类问题重试也没用，直接放弃
                logger.error("PushPlus 推送失败：%s", why)
                return False
            except requests.exceptions.RequestException as exc:
                logger.error("PushPlus 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_telegram(self, content, bot_token, chat_id):
        url = self.telegram_url.format(bot_token)
        payload = {"chat_id": chat_id, "text": content}

        try:
            response = requests.post(url, json=payload, proxies=self.proxies, timeout=30)
            logger.info("Telegram 响应: %s", response.text)
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram 代理发送失败: %s", exc)
            try:
                response = requests.post(url, json=payload, timeout=30)
                response.raise_for_status()
                return True
            except Exception as inner_exc:
                logger.error("Telegram 发送失败: %s", inner_exc)
                return False

    def push_wxpusher(self, content, spt):
        attempts = 5
        url = self.wxpusher_simple_url.format(spt, content)

        for attempt in range(attempts):
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                logger.info("WxPusher 响应: %s", response.text)
                ok, why = _check_code(response, (1000, "1000"), "WxPusher")
                if ok:
                    return True
                logger.error("WxPusher 推送失败：%s", why)
                return False
            except requests.exceptions.RequestException as exc:
                logger.error("WxPusher 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_serverChan(self, content, spt, is_success):
        attempts = 5
        url = self.server_chan_url.format(spt)

        title = f"微信阅读-{'成功' if is_success else '失败'}"

        for attempt in range(attempts):
            try:
                response = requests.post(
                    url,
                    data=json.dumps({"title": title, "desp": content}).encode("utf-8"),
                    headers=self.headers,
                    timeout=10,
                )
                response.raise_for_status()
                logger.info("ServerChan 响应: %s", response.text)
                ok, why = _check_code(response, (0, "0"), "ServerChan")
                if ok:
                    return True
                logger.error("ServerChan 推送失败：%s", why)
                return False
            except requests.exceptions.RequestException as exc:
                logger.error("ServerChan 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_wework(self, content, webhook, msg_type="markdown", is_success=True):
        """企业微信群机器人推送

        webhook: 形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx
        msg_type: markdown / text
        """
        attempts = 5
        if not webhook:
            logger.error("企业微信 Webhook 未配置，跳过推送。")
            return False

        # 加个状态标记，让消息更醒目
        flag = "✅" if is_success else "❌"

        if msg_type == "text":
            payload = {"msgtype": "text", "text": {"content": f"{flag} {content}"}}
        else:
            # markdown 模式下确保标题行加粗
            md_content = f"**{flag} 微信读书任务通知**\n{content}"
            payload = {"msgtype": "markdown", "markdown": {"content": md_content}}

        for attempt in range(attempts):
            try:
                response = requests.post(
                    webhook,
                    data=json.dumps(payload).encode("utf-8"),
                    headers=self.headers,
                    timeout=10,
                )
                response.raise_for_status()
                res = response.json()
                # 企业微信成功返回 {"errcode":0,"errmsg":"ok"}
                if res.get("errcode") == 0:
                    logger.info("企业微信推送成功: %s", response.text)
                    return True
                logger.error("企业微信推送返回错误: %s", response.text)
                return False
            except requests.exceptions.RequestException as exc:
                logger.error("企业微信推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(5, 15)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False


def push(content, method, is_success = True):
    notifier = PushNotification()

    if method in (None, ""):
        logger.warning("未配置推送渠道，跳过推送。")
        return False

    method = str(method).lower()

    if method == "pushplus":
        return notifier.push_pushplus(content, PUSHPLUS_TOKEN, is_success)
    if method == "telegram":
        return notifier.push_telegram(content, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    if method == "wxpusher":
        return notifier.push_wxpusher(content, WXPUSHER_SPT)
    if method == "serverchan":
        return notifier.push_serverChan(content, SERVERCHAN_SPT, is_success)
    if method == "wework":
        return notifier.push_wework(content, WEWORK_WEBHOOK, WEWORK_MSG_TYPE, is_success)

    logger.warning("无效的通知渠道 '%s'，已跳过推送。支持：pushplus、telegram、wxpusher、serverchan、wework", method)
    return False
