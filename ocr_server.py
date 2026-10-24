"""NEEA 验证码本地打码服务。

脚本 (ielts_auto_hook.user.js) 期望的协议：
  GET  /health        -> {"status": "ok"}
  POST /ocr/b64       -> 请求体 {"image": "<base64>"}，返回 {"success": bool, "code": "<4位大写字母>"}

原仓库自带的 captcha_server_ocr.py 只有 /ocr?url= 接口，与脚本不匹配，故重写。
"""
import base64
import logging
import os

from flask import Flask, jsonify, request
from flask_cors import cross_origin
import ddddocr

app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.WARNING)

ocr_beta = ddddocr.DdddOcr(beta=True, show_ad=False)
ocr_old = ddddocr.DdddOcr(beta=False, show_ad=False)


def process_answer(input_str):
    # NEEA 验证码为 4 位大写字母；识别结果混入数字时按形近字母纠正
    input_str = (input_str or '').strip()
    if len(input_str) != 4:
        return ""
    input_str = input_str.replace('0', 'O')
    input_str = input_str.replace('1', 'I')
    input_str = input_str.replace('5', 'S')
    input_str = input_str.replace('6', 'G')
    input_str = input_str.replace('8', 'B')
    if not all(c.isalpha() and c.isascii() for c in input_str):
        return ""
    return input_str.upper()


@app.route('/health')
@cross_origin()
def health():
    return jsonify({'status': 'ok'})


@app.route('/ocr/b64', methods=['POST'])
@cross_origin()
def ocr_b64():
    data = request.get_json(silent=True) or {}
    image_b64 = (data.get('image') or '').strip()
    if not image_b64:
        return jsonify({'success': False, 'code': '', 'error': 'no image'})

    if image_b64.startswith('data:'):
        image_b64 = image_b64.split(',', 1)[-1]

    try:
        content = base64.b64decode(image_b64)
    except Exception:
        return jsonify({'success': False, 'code': '', 'error': 'bad base64'})

    for ocr in (ocr_beta, ocr_old):
        try:
            raw = ocr.classification(content)
        except Exception as exc:
            app.logger.error('classification failed: %s', exc)
            continue
        app.logger.info('raw=%s', raw)
        answer = process_answer(raw)
        if answer:
            return jsonify({'success': True, 'code': answer})

    return jsonify({'success': False, 'code': '', 'error': 'unrecognized'})


if __name__ == '__main__':
    # 容器内需绑定 0.0.0.0 才能被端口映射转发；本地 bat 默认仍只听 127.0.0.1
    app.run(host=os.environ.get('OCR_HOST', '127.0.0.1'), port=9898)
