"""放行判定的视觉提示词消融实验。

背景：一张"鹈鹕骑车但脚悬在踏板上方"的截图，用当前提示词判 motion_mismatch
只有 0.462（vs consistent 0.508），基本是抛硬币；把脚部放大 3 倍后升到 0.816。
说明模型不是看不见图，而是"判断画面自不自洽"这个问题对它太抽象——它需要一个
更具体的核对动作，而不是一句"违反物理就 reject"。

这里并排跑几种写法，用同一张图比 margin（第一名减第二名），并混入一张正常
截图看会不会开始乱打回。运行：
    PYTHONPATH=. python tools/jev_prompt_lab.py <缺陷图> [正常图]
"""
import sys
import time
from pathlib import Path

from core.config import load_config
from core.jev import JevClient

DEFECT = "做一个鹈鹕骑自行车的动画，双脚跟着踏板踩动，轮子同步转动"
ACCEPTANCE = {
    "program_checks": {"gates": "passed", "tests": 1, "captures": 1,
                       "frames": 3, "dynamic_detected": True,
                       "motion_expected": ["bike.html"]},
    "reply_delivery": "prepared",
    "visual_evidence": {"source": "frames", "count": 3, "order": "按时间先后排列"},
}
REPLY = "动画已完成：鹈鹕蹬着踏板，车轮跟着转。"

# 所有变体共用同一份 state，只改提问部分，保证差异只来自提示词。
BASE_STATE = {
    "goal": DEFECT,
    "acceptance": ACCEPTANCE,
    "modified_files": ["bike.html"],
    "reply": REPLY,
    "evidence_boundary": "本轮只提供目标、验收标准、改过的文件名、最终回复与截图。",
    "images_note": "共 3 张截图，按时间先后排列。",
}

OPTIONS = ["pass", "reject"]
GAPS = ["none", "motion_mismatch", "visual_mismatch", "not_tested",
        "incomplete_work", "unsupported_claim", "off_target"]

VARIANTS = {}

# ---------------------------------------------------------------- V0 现基线
VARIANTS["V0 现基线"] = ({}, {
    "verdict": {"type": "choice", "options": OPTIONS,
                "instructions": "判断这次交付能否放行。pass表示回复内容与目标一致。acceptance.program_checks.motion_expected 非空表示这页本来就该有持续运动：请逐帧看图片，动作与物理规律矛盾或前后不连续就reject并选motion_mismatch，即使帧间确有像素变化。图片看不清的细节不要臆测。"},
    "gap": {"type": "choice", "options": GAPS,
            "instructions": "只在reject时选最主要的一项。motion_mismatch：画面里的动作自相矛盾或违反物理，例如脚没有踩在踏板上、脚与踏板转动不同步、轮子不转却前进、关节脱节或穿模。pass时选none。"},
})

# ------------------------------------------------- V1 清单式：给出核对项
CHECKLIST = [
    "脚（爪/蹄）与它踩的踏板是否接触：脚底和踏板之间有没有背景色缝隙",
    "手（翼/爪）与它握的车把、操纵杆是否接触",
    "关节和连接处有没有断开、错位、穿模",
    "该转的轮子/该走的指针有没有转，转向与前进方向是否一致",
    "多帧之间同一部件的位置变化是否连续，有没有突然跳位",
]
VARIANTS["V1 清单式"] = ({"visual_checklist": CHECKLIST}, {
    "verdict": {"type": "choice", "options": OPTIONS,
                "instructions": "请按 visual_checklist 逐项核对图片：只要有一项对不上就 reject，五项都对得上才 pass。核对时只看图片本身，不要假设动画应该是什么样。acceptance.program_checks.gates 为 passed 表示测试与截图已由程序核对通过，不要因此跳过看图。"},
    "gap": {"type": "choice", "options": GAPS,
            "instructions": "reject时选最主要的一项：motion_mismatch 用于 checklist 里任何一项接触/连接/转动对不上；visual_mismatch 只在目标明确写了颜色、布局、元素、文案而画面不符时才用。pass时选none。"},
})

# ------------------------------- V2 找茬式：先假定这批交付有问题
VARIANTS["V2 找茬式"] = ({}, {
    "verdict": {"type": "choice", "options": OPTIONS,
                "instructions": "这批交付很可能是带瑕疵提交的，请以找茬的心态看图：先找\"该接触却分开\"的地方——脚与踏板、手与车把、轮子与地面、关节与关节；再找\"该动却没动\"或\"动了但方向不对\"的地方。找到任意一处具体瑕疵就 reject；确实一处都找不到才 pass。不要因为整体看起来像那么回事就放过细节。"},
    "gap": {"type": "choice", "options": GAPS,
            "instructions": "reject时选最主要的一项。contact 类瑕疵（脚悬空、手没握住、部件脱节穿模）一律选 motion_mismatch。pass时选none。"},
})

# --------------------- V3 拆问题：先单独问接触关系，再问放行
VARIANTS["V3 拆问题"] = ({"visual_contact": "脚与踏板、手与车把、关节与关节的接触情况"}, {
    "contact": {"type": "choice",
                "options": ["all_contacted", "something_floating", "unclear"],
                "instructions": "只看图片回答接触关系：画面里正在操作物体的肢体（脚-踏板、手-车把或操纵杆、四肢-躯干）是否都连在一起？看到任何一处肢体与它该接触的物体之间存在背景色缝隙（悬空、脱开），选 something_floating；全部贴合选 all_contacted；画面太小看不清选 unclear。"},
    "verdict": {"type": "choice", "options": OPTIONS,
                "instructions": "contact 为 something_floating 时判 reject，contact 为 all_contacted 且目标达成就 pass。"},
    "gap": {"type": "choice", "options": GAPS,
            "instructions": "reject时选最主要的一项；contact 类问题选 motion_mismatch。pass时选none。"},
})

# --------------------- V4 单点聚焦：只问最典型的那一处
VARIANTS["V4 单点聚焦"] = ({}, {
    "verdict": {"type": "choice", "options": OPTIONS,
                "instructions": "先只做一个判断：图中角色的脚有没有踩在踏板（或它该踩的支撑面）上。判断方法：把注意力放到脚底与踏板之间，看那一小块区域的颜色是不是背景色——有背景色缝隙就是悬空，悬空即 reject 并选 motion_mismatch。脚确实贴在踏板上，再看轮子转动方向与前进方向是否一致、关节有没有脱节；都没问题才 pass。"},
    "gap": {"type": "choice", "options": GAPS,
            "instructions": "reject时选最主要的一项。脚悬空、转动不同步、关节脱节、穿模都选 motion_mismatch。pass时选none。"},
})

# --------------------- V5 正向表述：把\"一致\"说成需要证明的事
VARIANTS["V5 需自证"] = ({}, {
    "verdict": {"type": "choice", "options": OPTIONS,
                "instructions": "只有能逐条说出证据时才 pass。请在心里回答三个问题并据此判定：(1) 每个肢体与它操作的物体在哪接触，接触点画面上看得到吗；(2) 需要转动的部件在这几帧里转动了吗，方向对吗；(3) 有没有哪个部件悬空、脱节或穿模。任一问题答不上来或答案是否定的，判 reject。含糊的\"看起来还行\"不算证据。"},
    "gap": {"type": "choice", "options": GAPS,
            "instructions": "reject时选最主要的一项。(1)(2)(3) 中任何一项不成立都选 motion_mismatch。pass时选none。"},
})


# --------------------- V6 = V5 的自证要求 + V1 的具体核对项
VARIANTS["V6 自证+清单"] = ({"visual_checklist": CHECKLIST}, {
    "verdict": {"type": "choice", "options": OPTIONS,
                "instructions": "只有能把 visual_checklist 每一条都说清\"对上了\"才 pass。逐条核对：脚与踏板、手与车把、关节连接、转动方向、帧间连续性——每一条都要在画面上找得到对应位置；有一条说不清、或图上明显对不上，就 reject。含糊的\"看起来还行\"不算证据。"},
    "gap": {"type": "choice", "options": GAPS,
            "instructions": "reject时选最主要的一项。checklist 中任何一条对不上都选 motion_mismatch。pass时选none。"},
})


# ------------- V7：V5 + 静态分支（画面里没有肢体/机械结构时换核对方式）
VARIANTS["V7 自证+静态分支"] = ({}, {
    "verdict": {"type": "choice", "options": OPTIONS,
                "instructions": "先看画面里有没有会动的角色或机械结构。有的话，逐一确认：每个肢体在哪接触它操作的物体、接触点在画面上看得到吗；该转的部件转了吗、方向对吗；有没有部件悬空、脱节、穿模。答不上来或答案是否定的就 reject。画面里没有这类结构（纯文字、按钮、图表、静态插画）时，改成核对目标要求的元素是否都在、有无明显错位；齐全且无错位就 pass。含糊的\"看起来还行\"不算证据。"},
    "gap": {"type": "choice", "options": GAPS,
            "instructions": "reject时选最主要的一项。接触/连接/转动类问题选 motion_mismatch；目标明确写了颜色、布局、元素、文案而画面不符选 visual_mismatch。pass时选none。"},
})

# ------------- V8：V5 + 强制指认位置（定位不了就不能算\"看过\"）
VARIANTS["V8 需指认位置"] = ({}, {
    "verdict": {"type": "choice", "options": OPTIONS,
                "instructions": "判定前先在心里指出具体位置：角色的脚踩在哪个物体上、那个接触点在图上的哪个区域；它的手抓着什么；哪几个部件在转动。如果这些位置你指不出来，说明画面里这部分有问题（悬空、被挡住、根本没画），判 reject。三样都能指出、且彼此关系说得通，才 pass。只看整体像不像、不看接触点，容易放过悬空和脱节。"},
    "gap": {"type": "choice", "options": GAPS,
            "instructions": "reject时选最主要的一项。指不出接触点、悬空、脱节、穿模、转动方向不对都选 motion_mismatch。pass时选none。"},
})

# ------------- V9：V5 + 帧间连续性单列（多帧时才有意义）
VARIANTS["V9 自证+帧连续性"] = ({}, {
    "verdict": {"type": "choice", "options": OPTIONS,
                "instructions": "按三步判定。一、接触：每个肢体在哪接触它操作的物体，接触点在图上看得见吗；看不见（悬空、断开）就是缺陷。二、传动：该转的轮子/曲柄/指针有没有跟着转，方向与前进方向一致吗；不转或反了就反对。三、连续：多帧之间同一部件的位移是否连贯，有没有突然跳位或一帧动一帧不动。三步都通过才 pass；某一步说不出证据也判 reject。含糊的整体印象不算证据。"},
    "gap": {"type": "choice", "options": GAPS,
            "instructions": "reject时选最主要的一项。接触、传动、连续任一步不通过都选 motion_mismatch。pass时选none。"},
})


# ------------- V10：V5 + 看不清时的兜底（防止低分辨率被硬判缺陷）
VARIANTS["V10 自证+看不清兜底"] = ({}, {
    "verdict": {"type": "choice", "options": OPTIONS,
                "instructions": "只有能逐条说出证据时才 pass。请在心里回答三个问题并据此判定：(1) 每个肢体与它操作的物体在哪接触，接触点画面上看得到吗；(2) 需要转动的部件在这几帧里转动了吗，方向对吗；(3) 有没有哪个部件悬空、脱节或穿模。任一问题答案是否定的，判 reject。若是图片分辨率不足以看清接触点（不等于看得到却不合理），按整体关系判断，不要仅因模糊而 reject。含糊的\"看起来还行\"不算证据。"},
    "gap": {"type": "choice", "options": GAPS,
            "instructions": "reject时选最主要的一项。(1)(2)(3) 中任何一项不成立都选 motion_mismatch；只有目标明确写了颜色、布局、文字而画面不符才选 visual_mismatch。pass时选none。"},
})


def load(path):
    return Path(path).read_bytes()


def run(jev, name, images, state_extra, questions, rounds=2, state_override=None):
    state = dict(BASE_STATE)
    state.update(state_extra)
    if state_override:
        state.update(state_override)
    for i in range(rounds):
        started = time.monotonic()
        try:
            raw = jev.decide_raw(state, questions, images=images)
        except Exception as exc:
            print(f"  {name:12} run{i+1}: 请求失败 {exc}")
            continue
        answers = raw.get("answers") or {}
        verdict = answers.get("verdict") or {}
        probs = verdict.get("probabilities") or {}
        top = max(probs.values()) if probs else 0.0
        second = sorted(probs.values())[-2] if len(probs) > 1 else 0.0
        margin = top - second
        detail = ""
        if "contact" in answers:
            detail = " contact=" + str((answers["contact"] or {}).get("choice"))
        if "gap" in answers:
            detail += " gap=" + str((answers["gap"] or {}).get("choice"))
        print(f"  {name:12} run{i+1}: verdict={(verdict.get('choice'))}"
              f"  margin={margin:.3f}  p_pass={probs.get('pass', 0):.3f}"
              f"  p_reject={probs.get('reject', 0):.3f}{detail}"
              f"  {round((time.monotonic() - started) * 1000)}ms")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cfg = load_config().get("jev") or {}
    jev = JevClient(base_url=cfg.get("base_url"), api_key=cfg.get("api_key"),
                    model=cfg.get("model"))
    defect = load(sys.argv[1])
    normal = load(sys.argv[2]) if len(sys.argv) > 2 else None
    print(f"缺陷图 {len(defect)} 字节；对照图 {'有' if normal else '无'}\n")
    print("=== 缺陷图（期望 reject / motion_mismatch，margin 越高越好）===")
    for name, (extra, questions) in VARIANTS.items():
        run(jev, name, [defect, defect, defect], extra, questions)
    if normal:
        # 对照场景换成"静态落地页"：这批交付本来就没有运动，正确答案是 pass。
        # 若某个变体在这里也 reject，说明它被教成了逢图必挑刺。
        static_state = {
            "goal": "做一个产品落地页：标题、三行介绍、一个主按钮",
            "acceptance": {
                "program_checks": {"gates": "passed", "tests": 1, "captures": 1,
                                   "frames": 1},
                "reply_delivery": "prepared",
                "visual_evidence": {"source": "single", "count": 1},
            },
            "modified_files": ["landing.html"],
            "reply": "落地页做好了：卡片式布局，标题、三行介绍和蓝色主按钮都在。",
        }
        print("\n=== 静态落地页（期望 pass，用来看提示词是否开始乱打回）===")
        for name, (extra, questions) in VARIANTS.items():
            run(jev, name, [normal], extra, questions, rounds=1,
                state_override=static_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
