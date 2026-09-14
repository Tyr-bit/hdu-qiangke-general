# -*- coding: utf-8 -*-
"""
杭电通用抢课脚本（交互式）
========================================
功能:
  1. 交互输入学生学号、密码, 通过杭电统一认证(sso.hdu.edu.cn)登录教务系统
  2. 输入课程关键词(课程号或名称), 从选课系统检索课程, 支持一次添加多门课程
  3. 每门课程可自选目标教学班(按序号多选, 回车=全选), 或换关键词重搜
  4. 汇总所有目标后高频轮询提交, 抢到任意一门即调用"已选课程"接口核验并停止
  5. 已选课程自动跳过, 不会重复抢
适用: 杭州电子科技大学新版教务(jwglxt) 自主选课
用法:
  python hdu_qiangke_general.py                 # 交互式运行
  python hdu_qiangke_general.py --once          # 只提交一轮(用于测试)
  python hdu_qiangke_general.py --interval 1 --max 0   # 间隔1秒, 无限轮询(默认)
依赖: 仅需 Python 3.8+ 与 requests 库  (pip install requests)
"""
import argparse
import datetime
import getpass
import json
import os
import random
import re
import sys
import time

import requests

SSO = "https://sso.hdu.edu.cn"
JW = "https://newjw.hdu.edu.cn"
SERVICE = "http://newjw.hdu.edu.cn/sso/driot4login"
SVC_Q = requests.utils.quote(SERVICE, safe="")

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "qiangke_general_%s.log"
                        % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))

# 全局兜底参数(当前学期第二轮自主选课已验证的值, 优先使用页面动态参数)
FALLBACK = {
    "xkkz_id": "53B8BA0030E70B59E063DF64A8C0EF4C",
    "kklxdm": "01",
    "njdm_id": "2024",
    "zyh_id": "0523",
    "rwlx": "1",
    "xklc": "2",
    "xkxnm": "2026",
    "xkxqm": "3",
}


def log(msg):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def login(username, password, timeout=25):
    """杭电统一认证登录, 返回带登录态的 Session"""
    s = requests.Session()
    s.headers.update(UA)
    r0 = s.get(SSO + "/login?service=" + SVC_Q, timeout=timeout, allow_redirects=True)
    m = re.search(r'id="login-page-flowkey">([^<]*)', r0.text)
    if not m:
        raise RuntimeError("登录页解析失败(无 flowkey), 请检查网络")
    execution = m.group(1).strip()
    r = s.post(SSO + "/login?service=" + SVC_Q,
               data={"execution": execution, "_eventId": "submit",
                     "type": "UsernamePassword", "username": username,
                     "password": password, "geolocation": ""},
               headers={"Origin": SSO, "Referer": r0.url},
               allow_redirects=False, timeout=timeout)
    if r.status_code != 302 or "ticket=" not in r.headers.get("Location", ""):
        raise RuntimeError("登录失败: 学号或密码错误(%s)" % r.status_code)
    s.get(r.headers["Location"], timeout=timeout, allow_redirects=True)
    if "JSESSIONID" not in {c.name for c in s.cookies}:
        raise RuntimeError("登录后未获得会话, 请重试")
    return s


def get_index(s):
    """获取选课首页, 返回 (csrftoken, referer, hidden参数dict)"""
    idx = JW + "/jwglxt/xsxk/zzxkyzb_cxZzxkYzbIndex.html?gnmkdm=N253512&layout=default"
    r = s.get(idx, timeout=30)
    if r.status_code != 200:
        raise RuntimeError("选课首页异常: %s" % r.status_code)
    html = r.text
    hidden = {}
    for mm in re.finditer(r'<input[^>]*\bname="([^"]+)"[^>]*\bvalue="([^"]*)"', html):
        hidden[mm.group(1)] = mm.group(2)
    for mm in re.finditer(r'<input[^>]*\bvalue="([^"]*)"[^>]*\bname="([^"]+)"', html):
        hidden[mm.group(2)] = mm.group(1)
    m = re.search(r'name="csrftoken"\s+value="([^"]*)"', html)
    if not m:
        raise RuntimeError("选课首页无 csrftoken")
    return m.group(1), idx, hidden


def headers_for(referer):
    return {
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": JW,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": UA["User-Agent"],
    }


def get_jxb(s, hidden, referer, kch_id, kcmc):
    """获取某课程的全部教学班(教师/时间/容量/已选)"""
    params = {
        "rwlx": hidden.get("firstRwlx") or hidden.get("rwlx") or FALLBACK["rwlx"],
        "bklx_id": hidden.get("firstBklxId", ""),
        "xqh_id": hidden.get("firstXqhId", ""),
        "zyh_id": hidden.get("firstZyhId") or hidden.get("zyh_id") or FALLBACK["zyh_id"],
        "njdm_id": hidden.get("firstNjdmId") or hidden.get("njdm_id") or FALLBACK["njdm_id"],
        "xkxnm": hidden.get("xkxnm") or FALLBACK["xkxnm"],
        "xkxqm": hidden.get("xkxqm") or FALLBACK["xkxqm"],
        "kklxdm": hidden.get("kklxdm") or FALLBACK["kklxdm"],
        "kch_id": kch_id,
        "xkkz_id": hidden.get("xkkz_id") or FALLBACK["xkkz_id"],
        "xszxzt": "0", "cxbj": "0", "fxbj": "0",
        "csrftoken": hidden.get("csrftoken", ""),
    }
    r = s.post(JW + "/jwglxt/xsxk/zzxkyzbjk_cxJxbWithKchZzxkYzb.html",
               data=params, headers=headers_for(referer), timeout=30)
    try:
        data = r.json()
    except Exception:
        raise RuntimeError("教学班查询返回异常: %s" % r.text[:200])
    if isinstance(data, dict):
        raise RuntimeError("教学班查询失败: %s" % str(data.get("msg", data))[:200])
    return data


def teacher_name(jsxx):
    if not jsxx:
        return "?"
    parts = str(jsxx).split("/")
    return parts[1] if len(parts) > 1 else jsxx


def make_submit_form(hidden, tgt, csrftoken):
    """按课程构建提交参数(动态页面参数 + 已验证兜底)"""
    return {
        "jxb_ids": tgt["jxb_id"],
        "kch_id": tgt["kch_id"],
        "kcmc": tgt["kcmc"],
        "rwlx": hidden.get("firstRwlx") or hidden.get("rwlx") or FALLBACK["rwlx"],
        "rlkz": "0",
        "rlzlkz": "1",
        "sxbj": "0",
        "xxkbj": "0",
        "qz": "0",
        "cxbj": "0",
        "xkkz_id": hidden.get("xkkz_id") or FALLBACK["xkkz_id"],
        "njdm_id": hidden.get("firstNjdmId") or hidden.get("njdm_id") or FALLBACK["njdm_id"],
        "zyh_id": hidden.get("firstZyhId") or hidden.get("zyh_id") or FALLBACK["zyh_id"],
        "kklxdm": hidden.get("kklxdm") or FALLBACK["kklxdm"],
        "xklc": hidden.get("xklc") or FALLBACK["xklc"],
        "xkxnm": hidden.get("xkxnm") or FALLBACK["xkxnm"],
        "xkxqm": hidden.get("xkxqm") or FALLBACK["xkxqm"],
        "csrftoken": csrftoken,
    }


def submit(s, hidden, tgt, csrftoken, referer):
    """提交一次选课, 返回 (疑似成功?, 响应文本)"""
    form = make_submit_form(hidden, tgt, csrftoken)
    url = JW + "/jwglxt/xsxk/zzxkyzb_xkBcZyZzxkYzb.html"
    r = s.post(url, params={"gnmkdm": "N253512", "layout": "default"},
               data=form, headers=headers_for(referer), timeout=30)
    text = r.text.strip()
    try:
        j = json.loads(text)
        flag = str(j.get("flag"))
        msg = str(j.get("msg", ""))
        if flag == "-1" and re.match(r"^0,", msg):
            return False, text          # 容量已满/不允许
        if flag == "0":
            return False, text          # 业务拒绝(如: 一门课程只能选一个教学班)
        ok = (flag == "1") or ("成功" in msg)
        return ok, text
    except Exception:
        return False, text[:300]


def check_choosed(s, referer, kch_ids):
    """通过已选课程接口核验; 返回 (totalResult, 已中的kch列表)"""
    r = s.post(JW + "/jwglxt/xsxk/zzxkyzb_cxZzxkYzbChoosed.html",
               data={}, headers={"Referer": referer,
                                 "X-Requested-With": "XMLHttpRequest",
                                 "Origin": JW}, timeout=30)
    t = r.text
    m = re.search(r'"totalResult":"(\d+)"', t)
    total = int(m.group(1)) if m else -1
    hit = [k for k in kch_ids if k in t]
    return total, hit, t[:300]


def pick_courses(s, hidden, referer):
    """交互选择课程 -> 返回 queue: {jxb_id: tgt}, tgt含 kch_id/kcmc/name/sksj 等"""
    queue = {}
    ordered = []
    course_no = 0
    while True:
        course_no += 1
        if course_no > 1:
            print()
        kw = input("输入课程号(如 A2301240), 直接回车结束添加: ").strip().upper()
        if not kw:
            break
        if not re.fullmatch(r"[A-Z0-9]+", kw):
            print("课程号应为字母+数字, 如 A2301240")
            continue
        try:
            jxbs = get_jxb(s, hidden, referer, kw, "")
        except Exception as e:
            print("查询课程 %s 失败: %s" % (kw, e))
            continue
        if not jxbs:
            print("课程 %s 暂无可用教学班(检查课程号或当前选课轮次)" % kw)
            continue
        kcmc = input("课程名(用于日志显示, 直接回车跳过): ").strip()
        if not kcmc:
            kcmc = kw
        xf = jxbs[0].get("xf", "")
        kcmc_fmt = "(%s)%s" % (kw, kcmc)
        if xf:
            kcmc_fmt += " - %s 学分" % xf
        print("  -- 课程 %s %s 的教学班(%d个):" % (kw, kcmc, len(jxbs)))
        for i, j in enumerate(jxbs, 1):
            name = teacher_name(j.get("jsxx"))
            sksj = str(j.get("sksj", "")).replace("<br/>", " / ")
            print("  [%d] %s | %s | 容量%s 已选%s" % (i, name, sksj,
                                                   j.get("jxbrl"), j.get("yxzrs")))
        csel = input("选择教学班: [序号逗号分隔] / [回车=全部] / [教师关键词, 如:朱红]: ").strip()
        targets = []
        if not csel:
            targets = jxbs
        elif re.fullmatch(r"[0-9][0-9,，\s]*", csel):
            for s_ in re.split(r"[,，\s]+", csel):
                if s_.isdigit() and 1 <= int(s_) <= len(jxbs):
                    targets.append(jxbs[int(s_) - 1])
        else:
            # 教师关键词过滤: 匹配教师姓名子串
            word = csel.replace(" ", "")
            targets = [j for j in jxbs if word in teacher_name(j.get("jsxx"))]
            if not targets:
                print("未找到教师包含 [%s] 的教学班" % csel)
        if not targets:
            print("未选择教学班, 课程 %s 不加入" % kw)
            continue
        added = 0
        for j in targets:
            jb = j["jxb_id"]
            if jb in queue:
                continue
            queue[jb] = {
                "jxb_id": jb,
                "kch_id": kw,
                "kcmc": kcmc_fmt,
                "name": teacher_name(j.get("jsxx")),
                "sksj": str(j.get("sksj", "")).replace("<br/>", " / "),
            }
            ordered.append(jb)
            added += 1
        print("  已加入 %d 个教学班" % added)
    return queue, ordered


def main():
    ap = argparse.ArgumentParser(description="杭电通用抢课脚本")
    ap.add_argument("--interval", type=float, default=2.0, help="提交间隔秒(随机抖动±40%%)")
    ap.add_argument("--max", type=int, default=0, help="最大尝试次数, 0=无限(默认)")
    ap.add_argument("--once", action="store_true", help="只提交一轮(测试用)")
    args = ap.parse_args()

    print("=" * 60)
    print("杭电通用抢课脚本 | v1.0 | 交互式")
    print("=" * 60)
    username = input("请输入学号: ").strip()
    if sys.stdin.isatty():
        password = getpass.getpass("请输入密码: ")
    else:
        # 非终端(管道/重定向)场景: getpass 会阻塞, 退化为明文输入(仅用于自动化测试)
        password = input("请输入密码: ").strip()

    s = login(username, password)
    log("登录成功")
    csrftoken, referer, hidden = get_index(s)
    log("已进入选课系统, csrftoken 正常")

    queue, ordered = pick_courses(s, hidden, referer)
    if not queue:
        print("未添加任何课程, 退出")
        return
    print()
    print("=" * 60)
    print("本轮抢课清单 (%d 个教学班):" % len(queue))
    seen = set()
    for jb in ordered:
        t = queue[jb]
        key = t["kch_id"]
        if key not in seen:
            print("  课程: %s %s" % (t["kch_id"], t["kcmc"]))
            seen.add(key)
        print("      [%s] %s | %s" % (t["name"], t["sksj"], t["jxb_id"]))
    print("=" * 60)
    print("日志文件: %s" % LOG_FILE)
    confirm = input("开始抢课? (y/N): ").strip().lower()
    if confirm != "y":
        print("已取消")
        return

    kch_ids = sorted({t["kch_id"] for t in queue.values()})
    try:
        total, hit, _ = check_choosed(s, referer, kch_ids)
        log("当前已选课程数 totalResult=%s, 已含目标课=%s(注: 该接口可能恒为0, 以提交响应为准)" % (total, hit))
        if hit:
            log("!!! 已选列表已包含目标课程 [%s], 无需再抢" % ",".join(hit))
            return
    except Exception as e:
        log("已选查询失败(忽略): %s" % e)

    csrftoken, referer, hidden = get_index(s)   # 重新取一次确保 token 有效
    attempt = 0
    success = False
    removed = set()                              # 已处理(已选/不可再选)的课程号
    infinite = args.max <= 0
    while infinite or attempt < args.max:
        attempt += 1
        for jb in ordered:
            tgt = queue[jb]
            if tgt["kch_id"] in removed:
                continue
            try:
                ok, resp = submit(s, hidden, tgt, csrftoken, referer)
                log("[#%d][%s][%s] 响应: %s" % (attempt, tgt["kch_id"], tgt["name"], resp[:150]))
                if ok:
                    log("!!! 提交返回疑似成功, 校验已选课程...")
                    time.sleep(1)
                    try:
                        total, hit, _ = check_choosed(s, referer, [tgt["kch_id"]])
                        log("已选校验 totalResult=%s 命中=%s" % (total, hit))
                        if hit:
                            log("抢课成功 [%s], 退出" % ",".join(hit))
                            success = True
                            break
                        log("疑似成功但已选未命中: 可能是系统提示, 继续抢")
                    except Exception as e:
                        log("已选校验失败: %s" % e)
                if "不可再选" in resp or "只能选一个" in resp:
                    removed.add(tgt["kch_id"])
                    log("课程 %s 已选或不可再选, 移出队列(%d/%d 门已处理)"
                        % (tgt["kch_id"], len(removed), len(set(kch_ids))))
                    if len(removed) >= len(set(kch_ids)):
                        log("所有目标课程均已处理, 退出")
                        success = True
                        break
                    continue
                if "未知异常" in resp:
                    try:
                        csrftoken, referer, hidden = get_index(s)
                        log("已刷新 csrftoken")
                    except Exception:
                        pass
            except requests.RequestException as e:
                log("[#%d][%s] 网络异常: %s" % (attempt, tgt["name"], e))
                time.sleep(2)
                try:
                    s = login(username, password)
                    csrftoken, referer, hidden = get_index(s)
                    log("已重新登录并刷新 token")
                except Exception as e2:
                    log("重新登录失败: %s" % e2)
            except Exception as e:
                log("[#%d][%s] 未知异常: %s" % (attempt, tgt["name"], e))

            base = args.interval * random.uniform(0.6, 1.4)
            time.sleep(base)
        if success:
            break
        if args.once:
            break
        if attempt % 20 == 0:
            log("已尝试 %d 次, 继续..." % attempt)

    if not success and not infinite:
        log("达到最大尝试次数 %d, 未成功。可再次运行本脚本继续抢。" % args.max)


if __name__ == "__main__":
    main()