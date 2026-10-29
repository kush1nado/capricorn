"""Расход по агентам за сессию — для /usage: ходы, токены, стоимость (если в конфиге есть prices)."""
from __future__ import annotations

Price = tuple[float, float, float]      # $ за 1M токенов: вход, вход из кэша, выход


def k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def price_for(prices: dict[str, Price], model: str) -> Price | None:
    """Цена по полному имени (провайдер/модель), иначе по имени модели без провайдера."""
    return prices.get(model) or prices.get(model.split("/", 1)[-1])


def cost(p: Price, tokens_in: int, tokens_cached: int, tokens_out: int) -> float:
    return (tokens_in * p[0] + tokens_cached * p[1] + tokens_out * p[2]) / 1_000_000


def usage_report(orch) -> str:
    cfg = orch.cfg
    priced = bool(cfg.prices)
    rows = [["АГЕНТ", "ХОДЫ", "ВХОД", "КЭШ", "ВЫХОД"] + (["$"] if priced else [])]
    tot = [0, 0, 0, 0]
    money, unknown = 0.0, False
    for name in orch.roster:
        a = orch.agents[name]
        row = [name, str(a.turns), k(a.tokens_in), k(a.tokens_cached), k(a.tokens_out)]
        for i, v in enumerate((a.turns, a.tokens_in, a.tokens_cached, a.tokens_out)):
            tot[i] += v
        if priced:
            p = price_for(cfg.prices, cfg.agents[name].model)
            if p is None:
                unknown = unknown or bool(a.tokens_in or a.tokens_out)
                row.append("—")
            else:
                c = cost(p, a.tokens_in, a.tokens_cached, a.tokens_out)
                money += c
                row.append(f"${c:.2f}")
        rows.append(row)
    total = ["итого", str(tot[0]), k(tot[1]), k(tot[2]), k(tot[3])]
    if priced:
        total.append(f"${money:.2f}" + ("+" if unknown else ""))   # «+» — часть цен неизвестна
    rows.append(total)
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths))).rstrip()
             for r in rows]
    if not priced:
        lines.append("стоимость: добавь в конфиг prices — $ за 1M токенов [вход, кэш, выход] по моделям")
    saved = [(n, orch.agents[n].compact_saved) for n in orch.roster if orch.agents[n].compact_saved]
    if saved:
        lines.append("сжатие контекста срезало: " + ", ".join(f"{n} {k(s)} симв." for n, s in saved))
    return "\n".join(lines)
