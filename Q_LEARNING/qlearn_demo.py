"""
Минимальный пример Q-обучения на сетке-лабиринте 6x6.

Среда:
  - Старт в (0,0), цель в (5,5)
  - Несколько "ям" (pits) с большим штрафом
  - Действия: вверх, вправо, вниз, влево
  - Награды: +10 за цель, -10 за яму, -0.1 за каждый шаг (стимулирует короткие пути)

Алгоритм — ровно тот, что в псевдокоде Википедии, плюс ε-жадная стратегия.

На выходе:
  - схема среды и финальная политика
  - эволюция Q-таблицы на 4 этапах обучения
  - кривые обучения (награда и число шагов за эпизод)
"""

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch
from matplotlib.colors import LinearSegmentedColormap

np.random.seed(7)

# ============================================================
# 1. СРЕДА
# ============================================================
class GridWorld:
    def __init__(self):
        self.H, self.W = 16, 16
        self.start = (0, 0)
        self.goal  = (6, 3)
        self.pits  = [(2, 2), (2, 3), (4, 1), (4, 2), (1,2), (7,8),(10,5)]
        # 0=вверх, 1=вправо, 2=вниз, 3=влево
        self.A = 4
        self.S = self.H * self.W

    def reset(self):
        self.pos = self.start
        return self._state()

    def _state(self):
        return self.pos[0] * self.W + self.pos[1]

    def step(self, action):
        r, c = self.pos
        if   action == 0: r = max(0, r - 1)
        elif action == 1: c = min(self.W - 1, c + 1)
        elif action == 2: r = min(self.H - 1, r + 1)
        elif action == 3: c = max(0, c - 1)
        self.pos = (r, c)

        if self.pos == self.goal:
            return self._state(), +10.0, True
        if self.pos in self.pits:
            return self._state(), -10.0, True
        return self._state(), -0.1, False


# ============================================================
# 2. Q-LEARNING
# ============================================================
def q_learning(env, n_episodes=20000, alpha=0.15, gamma=0.95,
               eps_start=1.0, eps_end=0.02, eps_decay_eps=1500,
               max_steps=200, snapshot_at=(0, 50, 300, 1500)):
    """Точный аналог псевдокода из Википедии + ε-жадная стратегия."""
    Q = np.zeros((env.S, env.A))
    rewards, lengths = [], []
    snapshots = {}

    for ep in range(n_episodes):
        # линейный спад ε: исследование → эксплуатация
        eps = max(eps_end, eps_start - (eps_start - eps_end) * ep / eps_decay_eps)

        s = env.reset()
        total_r, t = 0.0, 0
        for t in range(max_steps):
            # ε-жадный выбор действия
            if np.random.random() < eps:
                a = np.random.randint(env.A)
            else:
                a = int(np.argmax(Q[s]))

            s_next, r, done = env.step(a)

            # САМОЕ ВАЖНОЕ — обновление Беллмана:
            # Q[s,a] ← Q[s,a] + α (r + γ·max_a' Q[s',a'] - Q[s,a])
            td_target = r + gamma * np.max(Q[s_next])
            Q[s, a] += alpha * (td_target - Q[s, a])

            s = s_next
            total_r += r
            if done:
                break

        rewards.append(total_r)
        lengths.append(t + 1)
        if ep in snapshot_at:
            snapshots[ep] = Q.copy()

    snapshots[n_episodes - 1] = Q.copy()
    return Q, rewards, lengths, snapshots


# ============================================================
# 3. ВИЗУАЛИЗАЦИЯ
# ============================================================
ARROWS = {0: '↑', 1: '→', 2: '↓', 3: '←'}

def draw_env(ax, env, title=""):
    """Базовая сетка: старт, цель, ямы."""
    ax.set_xlim(-0.5, env.W - 0.5)
    ax.set_ylim(env.H - 0.5, -0.5)  # перевернутая ось Y — чтобы (0,0) был сверху-слева
    ax.set_xticks(range(env.W)); ax.set_yticks(range(env.H))
    ax.set_xticklabels([]); ax.set_yticklabels([])
    ax.grid(True, color='gray', linewidth=0.5, alpha=0.5)
    ax.set_aspect('equal')

    # Ямы
    for (r, c) in env.pits:
        ax.add_patch(Rectangle((c - 0.5, r - 0.5), 1, 1, facecolor='#d62728', alpha=0.7))
        ax.text(c, r, 'X', ha='center', va='center', fontsize=18,
                fontweight='bold', color='white')
    # Старт и цель
    ax.add_patch(Rectangle((env.start[1]-0.5, env.start[0]-0.5), 1, 1,
                           facecolor='#1f77b4', alpha=0.6))
    ax.text(env.start[1], env.start[0], 'S', ha='center', va='center',
            fontsize=16, fontweight='bold', color='white')
    ax.add_patch(Rectangle((env.goal[1]-0.5, env.goal[0]-0.5), 1, 1,
                           facecolor='#2ca02c', alpha=0.6))
    ax.text(env.goal[1], env.goal[0], 'G', ha='center', va='center',
            fontsize=16, fontweight='bold', color='white')
    ax.set_title(title)


def draw_value_heatmap(ax, env, Q, title=""):
    """Раскраска по V(s) = max_a Q(s,a)."""
    V = Q.max(axis=1).reshape(env.H, env.W)
    # затемним терминальные клетки для понятности
    cmap = LinearSegmentedColormap.from_list('vmap', ['#f0f0f0', '#fff7bc', '#fec44f', '#d95f0e'])
    im = ax.imshow(V, cmap=cmap, vmin=-2, vmax=10)

    ax.set_xticks(range(env.W)); ax.set_yticks(range(env.H))
    ax.set_xticklabels([]); ax.set_yticklabels([])
    ax.set_aspect('equal')

    # ямы и цель поверх
    for (r, c) in env.pits:
        ax.add_patch(Rectangle((c - 0.5, r - 0.5), 1, 1,
                               facecolor='#d62728', alpha=0.85))
        ax.text(c, r, 'X', ha='center', va='center', fontsize=12,
                fontweight='bold', color='white')
    ax.add_patch(Rectangle((env.start[1]-0.5, env.start[0]-0.5), 1, 1,
                           edgecolor='#1f77b4', facecolor='none', linewidth=2.5))
    ax.add_patch(Rectangle((env.goal[1]-0.5, env.goal[0]-0.5), 1, 1,
                           edgecolor='#2ca02c', facecolor='none', linewidth=2.5))

    # стрелки оптимальной (жадной) политики
    for r in range(env.H):
        for c in range(env.W):
            if (r, c) in env.pits or (r, c) == env.goal:
                continue
            s = r * env.W + c
            if Q[s].max() == 0 and Q[s].min() == 0:
                continue  # не были тут
            a = np.argmax(Q[s])
            ax.text(c, r, ARROWS[a], ha='center', va='center',
                    fontsize=14, color='black', fontweight='bold')

    ax.set_title(title)
    return im


def trace_greedy_path(env, Q, max_steps=50):
    s = env.reset()
    path = [env.pos]
    for _ in range(max_steps):
        a = int(np.argmax(Q[s]))
        s, r, done = env.step(a)
        path.append(env.pos)
        if done:
            break
    return path


# ============================================================
# 4. ЗАПУСК
# ============================================================
env = GridWorld()
Q, rewards, lengths, snaps = q_learning(env)

# (а) Среда + финальная политика и путь
fig, axes = plt.subplots(1, 2, figsize=(12, 5.5))
draw_env(axes[0], env, "Среда")
draw_value_heatmap(axes[1], env, Q, "Финальная политика и V(s)")
path = trace_greedy_path(env, Q)
ys = [p[0] for p in path]; xs = [p[1] for p in path]
axes[1].plot(xs, ys, '-', color='#1f77b4', lw=2.5, alpha=0.7)
axes[1].plot(xs, ys, 'o', color='#1f77b4', markersize=8, alpha=0.85)
plt.tight_layout()
plt.savefig('qlearn_env_and_policy.png', dpi=110, bbox_inches='tight')
plt.close()

# (б) Эволюция Q-таблицы
fig, axes = plt.subplots(1, 4, figsize=(18, 4.5))
snap_eps = sorted(snaps.keys())[:4]
last_ep = sorted(snaps.keys())[-1]
snap_eps = [snap_eps[0], snap_eps[1], snap_eps[2], last_ep]
titles = [f"Эпизод {e+1}" for e in snap_eps]
for ax, ep, ttl in zip(axes, snap_eps, titles):
    im = draw_value_heatmap(ax, env, snaps[ep], ttl)
cbar_ax = fig.add_axes([0.92, 0.18, 0.012, 0.68])
fig.colorbar(im, cax=cbar_ax, label='V(s) = max Q(s,·)')
plt.subplots_adjust(right=0.9, wspace=0.1)
plt.savefig('qlearn_evolution.png', dpi=110, bbox_inches='tight')
plt.close()

# (в) Кривые обучения
fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
w = 50
def smooth(x, w):
    x = np.array(x, dtype=float)
    if len(x) < w: return x
    return np.convolve(x, np.ones(w)/w, mode='valid')

axes[0].plot(rewards, alpha=0.25, color='#1f77b4', label='raw')
axes[0].plot(np.arange(w-1, len(rewards)), smooth(rewards, w),
             color='#1f77b4', lw=2, label=f'скользящее среднее ({w} эп.)')
axes[0].axhline(0, color='k', lw=0.5)
axes[0].set_xlabel('Эпизод'); axes[0].set_ylabel('Суммарная награда за эпизод')
axes[0].set_title('Кривая обучения: награда')
axes[0].legend(); axes[0].grid(alpha=0.3)

axes[1].plot(lengths, alpha=0.25, color='#d62728', label='raw')
axes[1].plot(np.arange(w-1, len(lengths)), smooth(lengths, w),
             color='#d62728', lw=2, label=f'скользящее среднее ({w} эп.)')
axes[1].set_xlabel('Эпизод'); axes[1].set_ylabel('Шагов в эпизоде')
axes[1].set_title('Кривая обучения: длина эпизода')
axes[1].set_yscale('log')
axes[1].legend(); axes[1].grid(alpha=0.3, which='both')
plt.tight_layout()
plt.savefig('qlearn_curves.png', dpi=110, bbox_inches='tight')
plt.close()

# ============================================================
# 5. КОНСОЛЬНЫЙ ВЫВОД
# ============================================================
print(f"Обучено за {len(rewards)} эпизодов\n")

# Сколько шагов делает агент по жадной политике в конце
greedy_path = trace_greedy_path(env, Q)
print(f"Длина жадного пути после обучения: {len(greedy_path)-1} шагов")
print(f"Путь: {greedy_path}\n")

# Средняя награда: до и после
print(f"Средняя награда первые 50 эп.:   {np.mean(rewards[:50]):+.2f}")
print(f"Средняя награда последние 50 эп.: {np.mean(rewards[-50:]):+.2f}\n")

# Q-таблица для старта
s0 = env.start[0] * env.W + env.start[1]
print("Q-значения в стартовой клетке (s=0):")
print(f"  ↑ (вверх):  {Q[s0, 0]:+.3f}")
print(f"  → (вправо): {Q[s0, 1]:+.3f}")
print(f"  ↓ (вниз):   {Q[s0, 2]:+.3f}")
print(f"  ← (влево):  {Q[s0, 3]:+.3f}")
print(f"  Лучшее действие: {ARROWS[int(np.argmax(Q[s0]))]}")
