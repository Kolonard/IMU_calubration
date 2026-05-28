"""
Применение обученной DQN к новым условиям без переобучения.

Обучаем сеть на одной карте, затем запускаем её на 5 разных вариантах
среды и смотрим, где она работает, а где нет. Это раскрывает, что именно
сеть "запомнила" и каковы пределы её обобщения.

Варианты:
  1) Оригинальная карта
  2) Все ямы убраны
  3) Ямы переставлены (не на пути агента)
  4) Яма прямо на пути агента
  5) Цель переехала в другой угол
"""

import numpy as np
import torch
import torch.nn as nn
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch
from collections import deque
import random

SEED = 0
np.random.seed(SEED); torch.manual_seed(SEED); random.seed(SEED)

# ============================================================
# 1. СРЕДА И DQN (то же, что в предыдущем скрипте)
# ============================================================
class GridWorld:
    def __init__(self, pits=None, goal=None, start=None):
        self.H, self.W = 6, 6
        self.start = start if start is not None else (2, 1)
        self.goal  = goal  if goal  is not None else (5, 4)
        self.pits  = list(pits) if pits is not None else [(2,2),(2,3),(4,1),(4,2)]
        self.A, self.S = 4, self.H * self.W
    def reset(self):
        self.pos = self.start
        return self._state()
    def _state(self):
        return self.pos[0] * self.W + self.pos[1]
    def step(self, action):
        r, c = self.pos
        if   action == 0: r = max(0, r-1)
        elif action == 1: c = min(self.W-1, c+1)
        elif action == 2: r = min(self.H-1, r+1)
        elif action == 3: c = max(0, c-1)
        self.pos = (r, c)
        if self.pos == self.goal: return self._state(), +10.0, True
        if self.pos in self.pits: return self._state(), -10.0, True
        return self._state(), -0.1, False

class QNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(2, 64), nn.ReLU(),
            nn.Linear(64, 64), nn.ReLU(),
            nn.Linear(64, 4),
        )
    def forward(self, x): return self.net(x)

def s2f(s, env):
    return [(s // env.W) / (env.H - 1), (s % env.W) / (env.W - 1)]

def train_dqn(env, n_eps=2500, gamma=0.95, lr=1e-3,
              batch_sz=64, replay_sz=10000, target_update_ep=20,
              eps_decay_eps=900):
    qnet, target = QNet(), QNet()
    target.load_state_dict(qnet.state_dict())
    opt = torch.optim.Adam(qnet.parameters(), lr=lr)
    replay = deque(maxlen=replay_sz)
    for ep in range(n_eps):
        eps = max(0.02, 1.0 - ep / eps_decay_eps)
        s = env.reset()
        for _ in range(200):
            x = torch.tensor(s2f(s, env), dtype=torch.float32)
            if random.random() < eps:
                a = random.randrange(env.A)
            else:
                with torch.no_grad(): a = int(qnet(x).argmax())
            s_next, r, done = env.step(a)
            replay.append((s2f(s, env), a, r, s2f(s_next, env), float(done)))
            s = s_next
            if len(replay) >= batch_sz:
                batch = random.sample(replay, batch_sz)
                S  = torch.tensor([b[0] for b in batch], dtype=torch.float32)
                A_ = torch.tensor([b[1] for b in batch], dtype=torch.long)
                R  = torch.tensor([b[2] for b in batch], dtype=torch.float32)
                S2 = torch.tensor([b[3] for b in batch], dtype=torch.float32)
                D  = torch.tensor([b[4] for b in batch], dtype=torch.float32)
                q_sa = qnet(S).gather(1, A_.unsqueeze(1)).squeeze(1)
                with torch.no_grad():
                    td = R + gamma * target(S2).max(1).values * (1 - D)
                loss = nn.functional.mse_loss(q_sa, td)
                opt.zero_grad(); loss.backward(); opt.step()
            if done: break
        if ep % target_update_ep == 0:
            target.load_state_dict(qnet.state_dict())
    return qnet

# ============================================================
# 2. ОБУЧЕНИЕ (на оригинальной карте, один раз)
# ============================================================
print("Обучение DQN на оригинальной карте...")
env_train = GridWorld()
qnet = train_dqn(env_train)
print("Готово.\n")

# ============================================================
# 3. ЗАПУСК НА РАЗНЫХ ВАРИАНТАХ (БЕЗ ПЕРЕОБУЧЕНИЯ)
# ============================================================
def greedy_action(qnet, env, s):
    with torch.no_grad():
        return int(qnet(torch.tensor(s2f(s, env), dtype=torch.float32)).argmax())

def run_with_trace(env, qnet, max_steps=60):
    s = env.reset()
    path = [env.pos]; total_r = 0.0; outcome = "max_steps"
    for t in range(max_steps):
        a = greedy_action(qnet, env, s)
        s, r, done = env.step(a); total_r += r
        path.append(env.pos)
        if done:
            outcome = "goal" if env.pos == env.goal else "pit"
            return path, total_r, t+1, outcome
    return path, total_r, max_steps, outcome

variants = [
    dict(name="1. Оригинал",
         pits=[(2,2),(2,3),(4,1),(4,2)], goal=(5,5)),
    dict(name="2. Все ямы убраны",
         pits=[], goal=(5,5)),
    dict(name="3. Ямы в других местах\n(но не на пути)",
         pits=[(1,3),(1,4),(2,5),(3,4)], goal=(5,5)),
    dict(name="4. Яма ПРЯМО на пути\nв (3,0)",
         pits=[(2,2),(2,3),(3,0),(4,2)], goal=(5,5)),
    dict(name="5. Цель в противоположном\nуглу (0,5)",
         pits=[(2,2),(2,3),(4,1),(4,2)], goal=(0,5)),
]

results = []
print("Запуск ОБУЧЕННОЙ сети на разных вариантах среды:")
print(f"{'Вариант':<40}{'Исход':<10}{'Шагов':>8}{'Награда':>10}")
print("-" * 70)
for v in variants:
    env_v = GridWorld(pits=v["pits"], goal=v["goal"])
    path, ret, steps, outcome = run_with_trace(env_v, qnet)
    results.append(dict(env=env_v, path=path, outcome=outcome,
                        ret=ret, steps=steps, name=v["name"]))
    icon = {"goal": "✓ дошёл", "pit": "✗ в яме", "max_steps": "✗ застрял"}[outcome]
    print(f"{v['name'].replace(chr(10), ' '):<40}{icon:<10}{steps:>8}{ret:>+10.2f}")
print()

# ============================================================
# 4. ВИЗУАЛИЗАЦИЯ
# ============================================================
def draw_variant(ax, env, path, outcome, title):
    ax.set_xlim(-0.5, env.W-0.5)
    ax.set_ylim(env.H-0.5, -0.5)
    ax.set_xticks(range(env.W)); ax.set_yticks(range(env.H))
    ax.set_xticklabels([]); ax.set_yticklabels([])
    ax.grid(True, color='gray', linewidth=0.5, alpha=0.5)
    ax.set_aspect('equal')

    # ямы
    for (r, c) in env.pits:
        ax.add_patch(Rectangle((c-0.5, r-0.5), 1, 1, facecolor='#d62728', alpha=0.85))
        ax.text(c, r, 'X', ha='center', va='center', fontsize=14,
                color='white', fontweight='bold')
    # старт и цель
    ax.add_patch(Rectangle((env.start[1]-0.5, env.start[0]-0.5), 1, 1,
                           facecolor='#1f77b4', alpha=0.5))
    ax.text(env.start[1], env.start[0], 'S', ha='center', va='center',
            fontsize=13, fontweight='bold', color='white')
    ax.add_patch(Rectangle((env.goal[1]-0.5, env.goal[0]-0.5), 1, 1,
                           facecolor='#2ca02c', alpha=0.5))
    ax.text(env.goal[1], env.goal[0], 'G', ha='center', va='center',
            fontsize=13, fontweight='bold', color='white')

    # траектория (цвет = исход)
    color = {'goal': '#2ca02c', 'pit': '#d62728', 'max_steps': '#ff7f0e'}[outcome]
    ys = [p[0] for p in path]; xs = [p[1] for p in path]
    ax.plot(xs, ys, '-', color=color, lw=2.5, alpha=0.6)
    ax.plot(xs, ys, 'o', color=color, markersize=7, alpha=0.8)
    # маркер финиша
    if outcome == 'pit':
        ax.plot(xs[-1], ys[-1], 'x', color='black', markersize=18, mew=3)
    icon = {'goal': '✓', 'pit': '✗ упал в яму', 'max_steps': '✗ застрял'}[outcome]
    ax.set_title(f"{title}\n{icon}", fontsize=10)

fig, axes = plt.subplots(1, 5, figsize=(20, 4.5))
for ax, res in zip(axes, results):
    draw_variant(ax, res['env'], res['path'], res['outcome'], res['name'])
plt.tight_layout()
plt.savefig('dqn_transfer.png', dpi=110, bbox_inches='tight')
plt.close()

# ============================================================
# 5. КАК "ВИДИТ" СЕТЬ — её представление о пространстве
# ============================================================
# Главный пойнт: сеть никогда не видит карту, только координаты.
# Покажем её Q-значения (V(s) = max Q) на пустой карте.
fig, axes = plt.subplots(1, 2, figsize=(11, 5))

# Слева: что сеть выучила (V на оригинальной карте)
Q_orig = np.zeros((env_train.H, env_train.W))
for r in range(env_train.H):
    for c in range(env_train.W):
        with torch.no_grad():
            x = torch.tensor([r/(env_train.H-1), c/(env_train.W-1)], dtype=torch.float32)
            Q_orig[r, c] = float(qnet(x).max())
im0 = axes[0].imshow(Q_orig, cmap='YlOrRd', vmin=0, vmax=10)
axes[0].set_title('Что выучила сеть: V(s)\n(определяется только координатой)')
for (r, c) in env_train.pits:
    axes[0].add_patch(Rectangle((c-0.5, r-0.5), 1, 1, facecolor='#444'))
    axes[0].text(c, r, 'X', ha='center', va='center', color='white', fontweight='bold')
axes[0].add_patch(Rectangle((4.5, 4.5), 1, 1, edgecolor='lime', facecolor='none', lw=3))
axes[0].text(5, 5, 'G', ha='center', va='center', fontweight='bold')
axes[0].set_xticks([]); axes[0].set_yticks([])
plt.colorbar(im0, ax=axes[0], fraction=0.046)

# Справа: фактический ВХОД сети — только две нормированные координаты
ax = axes[1]
ax.set_xlim(-0.1, 1.1); ax.set_ylim(1.1, -0.1)
ax.set_aspect('equal')
ax.set_xlabel('column / (W-1)'); ax.set_ylabel('row / (H-1)')
ax.set_title('Что сеть фактически видит:\nтолько 2 числа на входе')
# рисуем точки = состояния
for r in range(env_train.H):
    for c in range(env_train.W):
        ax.plot(c/(env_train.W-1), r/(env_train.H-1), 'o',
                markersize=12, color='#1f77b4', alpha=0.6)
ax.text(0.5, -0.07, "← сеть НЕ знает, где ямы\nи где цель — только позицию",
        ha='center', fontsize=10, style='italic')
ax.grid(alpha=0.3)
plt.tight_layout()
plt.savefig('dqn_what_it_sees.png', dpi=110, bbox_inches='tight')
plt.close()

print("Графики: dqn_transfer.png, dqn_what_it_sees.png")
