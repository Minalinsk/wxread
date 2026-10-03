import json
import logging
import os
import random
import re
import time

import requests

from config import (
    PUSHPLUS_TOKEN,
    SERVERCHAN_SPT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WECOM_MENTIONED,
    WECOM_MSGTYPE,
    WECOM_WEBHOOK,
    WXPUSHER_SPT,
)

logger = logging.getLogger(__name__)

# 企业微信机器人 Webhook 模板，填 key 时自动补全
WECOM_WEBHOOK_TEMPLATE = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={}"
# 企业微信 markdown 消息体上限为 4096 字节，留一点余量
WECOM_CONTENT_LIMIT = 4000
# 日志中需要脱敏的 URL 参数名
SENSITIVE_QUERY_KEYS = ("key", "token", "access_token", "secret", "sendkey")
# 用于清洗异常信息里夹带的完整 URL（requests 抛错时会带上原始地址）
SENSITIVE_QUERY_RE = re.compile(r"(?i)\b(key|token|access_token|secret|sendkey)=([^&\s'\"]+)")
# 企业微信允许的消息类型
WECOM_MSG_TYPES = ("markdown", "text")


def mask_secret(value, keep=2):
    """脱敏敏感字符串，避免 token/key 明文进入日志。"""
    value = str(value or "")
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep)}"


def safe_url(url):
    """日志展示用：隐藏 URL 中的 key/token 等敏感参数。"""
    url = str(url or "")
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    params = []
    for item in query.split("&"):
        name, sep, value = item.partition("=")
        if sep and name.lower() in SENSITIVE_QUERY_KEYS:
            params.append(f"{name}={mask_secret(value)}")
        else:
            params.append(item)
    return f"{base}?{'&'.join(params)}"


def safe_text(text):
    """日志展示用：把文本中夹带的 URL 敏感参数一并脱敏。

    requests 抛出的异常信息里会包含完整请求地址，必须先清洗再打日志。
    """
    return SENSITIVE_QUERY_RE.sub(lambda m: f"{m.group(1)}={mask_secret(m.group(2))}", str(text))


def normalize_wecom_webhook(webhook):
    """归一化企业微信机器人地址。

    支持三种填法：
    1. 完整地址：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
    2. 只填 key：xxxx-xxxx-xxxx-xxxx
    3. key=xxxx 形式
    """
    webhook = str(webhook or "").strip().strip('"').strip("'")
    if not webhook:
        return None
    if webhook.lower().startswith(("http://", "https://")):
        return webhook
    if webhook.lower().startswith("key="):
        webhook = webhook[4:].strip()
    if not webhook:
        return None
    return WECOM_WEBHOOK_TEMPLATE.format(webhook)


def normalize_wecom_mentioned(mentioned):
    """解析 @ 配置：支持 '@all' 或逗号/空格分隔的成员 userid。"""
    if not mentioned:
        return []
    if isinstance(mentioned, (list, tuple, set)):
        items = [str(item) for item in mentioned]
    else:
        items = re.split(r"[,\s;，；、]+", str(mentioned))
    return [item.strip() for item in items if item and item.strip()]


def truncate_wecom_content(content):
    """按字节截断，避免超出企业微信的长度限制。"""
    content = str(content)
    encoded = content.encode("utf-8")
    if len(encoded) <= WECOM_CONTENT_LIMIT:
        return content
    return encoded[: WECOM_CONTENT_LIMIT - 3].decode("utf-8", errors="ignore") + "..."


def build_wecom_payload(title, content, msgtype=None, mentioned=None):
    """构造企业微信机器人消息体。

    msgtype 为 markdown 时消息体带加粗标题；为 text 时支持 @ 指定成员。
    """
    msgtype = str(msgtype or "markdown").strip().lower()
    if msgtype not in WECOM_MSG_TYPES:
        logger.warning("不支持的 WECOM_MSGTYPE '%s'，已回退为 markdown。", msgtype)
        msgtype = "markdown"

    if msgtype == "text":
        payload = {
            "msgtype": "text",
            "text": {"content": truncate_wecom_content(f"{title}\n{content}")},
        }
        mentions = normalize_wecom_mentioned(mentioned)
        if mentions:
            payload["text"]["mentioned_list"] = mentions
        return payload

    return {
        "msgtype": "markdown",
        "markdown": {"content": truncate_wecom_content(f"**{title}**\n{content}")},
    }


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
                return True
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
                return True
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
                return True
            except requests.exceptions.RequestException as exc:
                logger.error("ServerChan 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_wecom(self, content, webhook, is_success=True, msgtype=None, mentioned=None):
        """企业微信（WeCom）群机器人推送。

        企业微信即使是业务失败也会返回 HTTP 200，必须解析响应体中的 errcode。
        - errcode == 0：成功
        - errcode == 45009：接口调用超过限制，属于临时错误，短暂等待后重试
        - 其它 errcode（如 93000 key 无效、40058 内容过长）：不可重试，直接失败
        """
        url = normalize_wecom_webhook(webhook)
        if not url:
            logger.warning("未配置企业微信机器人 Webhook，跳过推送。")
            return False

        attempts = 5
        title = f"微信阅读-{'成功' if is_success else '失败'}"
        payload = build_wecom_payload(title, content, msgtype, mentioned)
        log_url = safe_url(url)

        for attempt in range(attempts):
            try:
                response = requests.post(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers=self.headers,
                    timeout=10,
                )
                response.raise_for_status()
                result = response.json()
            except (requests.exceptions.RequestException, ValueError) as exc:
                logger.error("企业微信机器人推送失败（%s）：%s", log_url, safe_text(exc))
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
                continue

            errcode = result.get("errcode")
            if errcode == 0:
                logger.info("企业微信机器人响应: %s", result)
                return True

            logger.error(
                "企业微信机器人推送失败（%s）：errcode=%s，errmsg=%s",
                log_url,
                errcode,
                result.get("errmsg"),
            )
            if errcode != 45009:
                return False
            if attempt < attempts - 1:
                sleep_time = random.randint(60, 120)
                logger.info("触发频率限制，%d 秒后重试...", sleep_time)
                time.sleep(sleep_time)

        return False


def push(content, method, is_success = True):
    notifier = PushNotification()

    if method in (None, ""):
        logger.warning("未配置推送渠道，跳过推送。")
        return False

    method = str(method).lower().strip()

    if method == "pushplus":
        return notifier.push_pushplus(content, PUSHPLUS_TOKEN, is_success)
    if method == "telegram":
        return notifier.push_telegram(content, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    if method == "wxpusher":
        return notifier.push_wxpusher(content, WXPUSHER_SPT)
    if method == "serverchan":
        return notifier.push_serverChan(content, SERVERCHAN_SPT, is_success)
    if method in ("wecom", "wecombot", "qyweixin", "workweixin", "企业微信"):
        return notifier.push_wecom(content, WECOM_WEBHOOK, is_success, WECOM_MSGTYPE, WECOM_MENTIONED)

    logger.warning("无效的通知渠道 '%s'，已跳过推送。支持：pushplus、telegram、wxpusher、serverchan、wecom", method)
    return False
