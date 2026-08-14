# -*- coding: utf-8 -*-
"""
企业微信应用消息发送提醒服务

职责：
1. 通过企业微信「应用消息」API（corpid/agentid/secret -> gettoken -> message/send）推送通知
2. 支持文本(text)与 Markdown 两种消息类型，超长内容自动分片

与 wechat_sender.py（企业微信群机器人 Webhook）的区别：
- 群机器人消息只停留在企业微信 App 的群聊里，无法同步到个人微信；
- 应用消息走企业「微信插件」，可同步至个人微信（「企业微信通知」服务号），
  因此当目标是「在个人微信里收到推送」时，应使用本渠道。
- 应用消息要求发送方出口 IP 在该应用的「企业可信 IP」白名单内，否则 message/send
  会返回 errcode=60020。
"""
import logging
import time
from typing import Optional

import requests

from src.config import Config
from src.formatters import chunk_content_by_max_bytes, strip_hidden_markdown_metadata


logger = logging.getLogger(__name__)


# 企业微信应用消息单条字节上限：text/markdown 均为 2048 字节，预留余量。
WECHAT_WORK_APP_DEFAULT_MAX_BYTES = 2000

# access_token 提前过期阈值（秒），避免临界失效。
WECHAT_WORK_APP_TOKEN_REFRESH_LEAD_SECONDS = 300


class WechatWorkAppSender:

    def __init__(self, config: Config):
        """
        初始化企业微信应用消息配置

        Args:
            config: 配置对象
        """
        self._corpid = getattr(config, 'wechat_work_corpid', None)
        self._agentid = getattr(config, 'wechat_work_agentid', None)
        self._secret = getattr(config, 'wechat_work_secret', None)
        self._touser = getattr(config, 'wechat_work_touser', None) or '@all'
        self._msg_type = (getattr(config, 'wechat_work_msg_type', None) or 'text').lower()
        self._max_bytes = getattr(config, 'wechat_work_max_bytes', WECHAT_WORK_APP_DEFAULT_MAX_BYTES)

        # access_token 缓存
        self._access_token: Optional[str] = None
        self._token_expire_at: float = 0.0

    def send_to_wechat_work_app(
        self,
        content: str,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """
        推送消息到企业微信应用消息

        Args:
            content: Markdown 格式的消息内容（text 类型会以纯文本发送）

        Returns:
            是否发送成功（分片场景下要求全部批次成功）
        """
        if not (self._corpid and self._agentid and self._secret):
            logger.warning("企业微信应用消息配置不完整（corpid/agentid/secret），跳过推送")
            return False

        sanitized_content = strip_hidden_markdown_metadata(content).strip()
        if not sanitized_content:
            logger.warning("企业微信应用消息内容为空，跳过推送")
            return False

        # text 类型受 2048 字节限制约束得更紧
        max_bytes = self._max_bytes
        if self._msg_type == 'text':
            max_bytes = min(max_bytes, WECHAT_WORK_APP_DEFAULT_MAX_BYTES)

        try:
            content_bytes = len(sanitized_content.encode('utf-8'))
            if content_bytes > max_bytes:
                logger.info(
                    "企业微信应用消息内容超长(%s字节/%s字符)，将分批发送",
                    content_bytes,
                    len(sanitized_content),
                )
                return self._send_chunked(sanitized_content, max_bytes, timeout_seconds=timeout_seconds)

            return self._send_message(sanitized_content, timeout_seconds=timeout_seconds)
        except Exception as e:
            logger.error(f"发送企业微信应用消息失败: {e}")
            return False

    def _get_access_token(self, *, timeout_seconds: Optional[float] = None) -> Optional[str]:
        """获取企业微信 access_token，带缓存（提前 5 分钟过期复取）"""
        if self._access_token and time.time() < self._token_expire_at:
            return self._access_token

        url = (
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
            f"?corpid={self._corpid}&corpsecret={self._secret}"
        )
        try:
            response = requests.get(url, timeout=timeout_seconds or 10)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"获取企业微信 access_token 失败: {e}")
            return None

        if data.get('errcode') == 0:
            self._access_token = data.get('access_token')
            self._token_expire_at = (
                time.time()
                + int(data.get('expires_in', 7200))
                - WECHAT_WORK_APP_TOKEN_REFRESH_LEAD_SECONDS
            )
            return self._access_token

        logger.error(f"获取企业微信 access_token 失败: {data.get('errcode')} {data.get('errmsg')}")
        return None

    def _build_payload(self, content: str) -> dict:
        """生成企业微信应用消息 payload"""
        msg_type = self._msg_type
        if msg_type == 'markdown':
            return {
                "touser": self._touser,
                "msgtype": "markdown",
                "agentid": int(self._agentid),
                "markdown": {"content": content},
            }
        # 默认 text（在微信里可读性最好）
        return {
            "touser": self._touser,
            "msgtype": "text",
            "agentid": int(self._agentid),
            "text": {"content": content},
        }

    def _send_message(self, content: str, *, timeout_seconds: Optional[float] = None) -> bool:
        """发送单条企业微信应用消息"""
        access_token = self._get_access_token(timeout_seconds=timeout_seconds)
        if not access_token:
            return False

        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"
        payload = self._build_payload(content)

        try:
            response = requests.post(
                url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=timeout_seconds or 10,
            )
            response.raise_for_status()
            result = response.json()
        except Exception as e:
            logger.error(f"发送企业微信应用消息异常: {e}")
            return False

        if result.get('errcode') == 0:
            logger.info("企业微信应用消息发送成功")
            return True

        logger.error(
            f"企业微信应用消息发送失败: {result.get('errcode')} {result.get('errmsg')} "
            f"invaliduser={result.get('invaliduser')}"
        )
        return False

    def _send_chunked(
        self, content: str, max_bytes: int, *, timeout_seconds: Optional[float] = None
    ) -> bool:
        """分批发送长消息到企业微信应用消息，确保每批不超过字节限制"""
        chunks = chunk_content_by_max_bytes(content, max_bytes, add_page_marker=True)
        total_chunks = len(chunks)
        success_count = 0
        for i, chunk in enumerate(chunks):
            if self._send_message(chunk, timeout_seconds=timeout_seconds):
                success_count += 1
            else:
                logger.error(f"企业微信应用消息第 {i+1}/{total_chunks} 批发送失败")
            if i < total_chunks - 1:
                time.sleep(1)
        return success_count == total_chunks
