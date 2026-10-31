"""Small persisted chat games, without filesystem, shell or browser privileges."""
import json
import secrets
import time
from core.memory import MiniMemory


def play(args, scope="local:games", memory=None):
    game = args["game"]
    action = args.get("action", "start")
    if game == "dice":
        count, sides = args.get("count", 1), args.get("sides", 6)
        if type(count) is not int or type(sides) is not int or not 1<=count<=6 or not 2<=sides<=100:
            raise ValueError("骰子数量1到6，面数2到100")
        values = [secrets.randbelow(sides)+1 for _ in range(count)]
        return {"ok":True,"output":f"掷出 {count}d{sides}：{values}，合计 {sum(values)}"}
    memory = memory or MiniMemory()
    with memory._get_conn() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS chat_games (scope TEXT PRIMARY KEY,payload TEXT,updated_at REAL)")
        row = db.execute("SELECT * FROM chat_games WHERE scope=?", (scope,)).fetchone()
        state = json.loads(row["payload"]) if row and time.time()-row["updated_at"]<86400 else {}
        if action == "stop":
            db.execute("DELETE FROM chat_games WHERE scope=?", (scope,))
            return {"ok":True,"output":"这轮游戏结束啦。"}
        if game == "guess_number":
            if action == "start":
                state = {"game":game,"answer":secrets.randbelow(100)+1,"tries":0}
                output = "我想好了1到100的一个整数，来猜吧！最多12次，会提示大了还是小了。"
            else:
                if state.get("game") != game:
                    return {"ok":False,"error":"当前没有猜数字游戏，请先start"}
                number = args.get("number")
                if type(number) is not int or not 1<=number<=100:
                    raise ValueError("number 需要1到100的整数")
                state["tries"] += 1
                answer = state["answer"]
                if number == answer or state["tries"]>=12:
                    output = f"{'猜对啦' if number==answer else '次数用完啦'}！答案是{answer}，共猜了{state['tries']}次。"
                    state = {}
                else:
                    output = f"{'大了' if number>answer else '小了'}，还有{12-state['tries']}次机会。"
        elif game == "riddle":
            if action == "start":
                question, answer = secrets.choice([("什么东西越洗越脏？", "水"),("什么门永远关不上？", "球门"),("什么布剪不断？", "瀑布")])
                state = {"game":game,"answer":answer}
                output = question
            else:
                if state.get("game") != game:
                    return {"ok":False,"error":"当前没有谜语，请先start"}
                correct = state["answer"] in args.get("answer", "")
                output = "答对啦！" if correct else "还没猜中，再想想？"
                if correct:
                    state = {}
        else:
            raise ValueError("未知群聊游戏")
        db.execute("INSERT OR REPLACE INTO chat_games VALUES (?,?,?)", (scope,json.dumps(state,ensure_ascii=False),time.time()))
    return {"ok":True,"output":output}
