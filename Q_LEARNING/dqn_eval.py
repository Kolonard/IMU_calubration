"""
DQN на GridWorld 6x6 + методология проверки.

Сначала тренируем нейросеть (а не таблицу) Q-обучением.
Затем прогоняем 4 разных теста, каждый ловит свой вид проблем:

  Тест 1. Базовый: жадная политика из старта (sanity check)
  Тест 2. Робастность к начальной позиции (не переобучилась ли политика на путь от S?)
  Тест 3. Робастность к стохастике (что если среда не идеально детерминирована?)
  Тест 4. Калибровка Q-значений (предсказанная ценность совпадает с реальной?)
  Тест 5. Сравнение с оптимальной политикой (BFS даёт нижнюю границу)
"""

import numpy as np
import torch
import torch.nn as nn
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from collections import deque
import random

SEED = 0
np.random.seed(SEED); torch.manual_seed(SEED); random.seed(SEED)

# ============================================================
# 1. СРЕДА (та же, плюс опциональная стохастика)
# ============================================================
class GridWorld:
    def __init__(self, slip=0.0, pits=None):
        self.H, self.W = 6, 6
        self.start = (0, 0)
        self.goal  = (5, 5)
        self.pits  = list(pits) if pits is not None else [(2, 2), (2, 3), (4, 1), (4, 2)]
        self.A, self.S = 4, self.H * self.W
        self.slip = slip   # вероятность "поскользнуться" — действие заменяется случайным

    def reset(self, start=None):
        self.pos = start if start is not None else self.start
        return self._state()

    def _state(self):
        return self.pos[0] * self.W + self.pos[1]

    def step(self, action):
        if self.slip > 0 and random.random() < self.slip:
            action = random.randrange(self.A)
        r, c = self.pos
        if   action == 0: r = max(0, r - 1)
        elif action == 1: c = min(self.W - 1, c + 1)
        elif action == 2: r = min(self.H - 1, r + 1)
        elif action == 3: c = max(0, c - 1)
        self.pos = (r, c)
        if self.pos == self.goal: return self._state(), +10.0, True
        if self.pos in self.pits: return self._state(), -10.0, True
        return self._state(), -0.1, False


# ============================================================
# 2. DQN: маленькая MLP вместо Q-таблицы
# ============================================================
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
    """Состояние (индекс) → признаки (нормированные координаты)."""
    return [(s // env.W) / (env.H - 1), (s % env.W) / (env.W - 1)]

def train_dqn(env, n_eps=1200, gamma=0.95, lr=1e-3,
              batch_sz=64, replay_sz=10000, target_update_ep=20,
              eps_decay_eps=900):
    qnet, target = QNet(), QNet()
    target.load_state_dict(qnet.state_dict())
    opt = torch.optim.Adam(qnet.parameters(), lr=lr)
    replay = deque(maxlen=replay_sz)

    rewards, lengths, q_start_hist = [], [], []
    for ep in range(n_eps):
        eps = max(0.02, 1.0 - ep / eps_decay_eps)
        s = env.reset()
        total_r, t = 0.0, 0
        for t in range(200):
            x = torch.tensor(s2f(s, env), dtype=torch.float32)
            if random.random() < eps:
                a = random.randrange(env.A)
            else:
                with torch.no_grad():
                    a = int(qnet(x).argmax())
            s_next, r, done = env.step(a)
            replay.append((s2f(s, env), a, r, s2f(s_next, env), float(done)))
            total_r += r; s = s_next

            # шаг обучения
            if len(replay) >= batch_sz:
                batch = random.sample(replay, batch_sz)
                S  = torch.tensor([b[0] for b in batch], dtype=torch.float32)
                A_ = torch.tensor([b[1] for b in batch], dtype=torch.long)
                R  = torch.tensor([b[2] for b in batch], dtype=torch.float32)
                S2 = torch.tensor([b[3] for b in batch], dtype=torch.float32)
                D  = torch.tensor([b[4] for b in batch], dtype=torch.float32)
                q_sa = qnet(S).gather(1, A_.unsqueeze(1)).squeeze(1)
                with torch.no_grad():
                    td_target = R + gamma * target(S2).max(1).values * (1 - D)
                loss = nn.functional.mse_loss(q_sa, td_target)
                opt.zero_grad(); loss.backward(); opt.step()

            if done: break

        # обновление target-сети
        if ep % target_update_ep == 0:
            target.load_state_dict(qnet.state_dict())

        rewards.append(total_r); lengths.append(t + 1)
        with torch.no_grad():
            q_start_hist.append(qnet(torch.tensor(s2f(0, env), dtype=torch.float32)).numpy())

    return qnet, rewards, lengths, np.array(q_start_hist)


# ============================================================
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
def greedy_action(qnet, env, s):
    with torch.no_grad():
        return int(qnet(torch.tensor(s2f(s, env), dtype=torch.float32)).argmax())

def run_greedy(env, qnet, start=None, max_steps=60):
    s = env.reset(start=start); total_r = 0.0
    for t in range(max_steps):
        a = greedy_action(qnet, env, s)
        s, r, done = env.step(a); total_r += r
        if done:
            return total_r, t + 1, (env.pos == env.goal)
    return total_r, max_steps, False

def get_q_table(qnet, env):
    Q = np.zeros((env.S, env.A))
    for s in range(env.S):
        with torch.no_grad():
            Q[s] = qnet(torch.tensor(s2f(s, env), dtype=torch.float32)).numpy()
    return Q

def bfs_distance(env):
    """Минимальное число шагов от каждой клетки до цели (BFS)."""
    from collections import deque as dq
    dist = np.full((env.H, env.W), np.inf)
    dist[env.goal] = 0
    q = dq([env.goal])
    while q:
        r, c = q.popleft()
        for dr, dc in [(-1,0),(1,0),(0,-1),(0,1)]:
            nr, nc = r+dr, c+dc
            if 0 <= nr < env.H and 0 <= nc < env.W:
                if (nr, nc) in env.pits: continue
                if dist[nr, nc] == np.inf:
                    dist[nr, nc] = dist[r, c] + 1
                    q.append((nr, nc))
    return dist


# ============================================================
# 4. ТЕСТЫ
# ============================================================
def test_basic(env, qnet, n=300):
    """Тест 1: жадная политика из старта, много прогонов."""
    succ, lens, rets = 0, [], []
    for _ in range(n):
        r, l, s = run_greedy(env, qnet); succ += int(s); lens.append(l); rets.append(r)
    return succ/n, np.mean(lens), np.std(lens), np.mean(rets), np.std(rets)

def test_all_starts(env, qnet):
    """Тест 2: пробуем каждую клетку как старт."""
    succ = np.zeros((env.H, env.W))
    length = np.full((env.H, env.W), np.nan)
    for r in range(env.H):
        for c in range(env.W):
            if (r, c) in env.pits or (r, c) == env.goal:
                succ[r, c] = np.nan; continue
            _, l, s = run_greedy(env, qnet, start=(r, c))
            succ[r, c] = int(s); length[r, c] = l if s else np.nan
    return succ, length

def test_stochastic(qnet, slip_levels, n=300):
    """Тест 3: разные уровни «скольжения» в среде."""
    out = []
    for sl in slip_levels:
        env_t = GridWorld(slip=sl)
        sr, ml, sd, mr, sr2 = test_basic(env_t, qnet, n=n)
        out.append((sl, sr, ml, mr))
    return out

def test_calibration(env, qnet, n_traj=100, gamma=0.95):
    """Тест 4: Q(s,a) должно равняться фактическому дисконтированному return-to-go."""
    pred, actual = [], []
    # Стартуем из разных клеток для разнообразия
    starts = [(r, c) for r in range(env.H) for c in range(env.W)
              if (r, c) not in env.pits and (r, c) != env.goal]
    for trial in range(n_traj):
        start = starts[trial % len(starts)]
        s = env.reset(start=start)
        traj_s, traj_a, traj_r = [], [], []
        for t in range(60):
            a = greedy_action(qnet, env, s)
            traj_s.append(s); traj_a.append(a)
            s, r, done = env.step(a); traj_r.append(r)
            if done: break
        T = len(traj_r)
        if T == 0: continue
        # Дисконтированный возврат от каждого шага
        G = np.zeros(T); G[-1] = traj_r[-1]
        for t in range(T - 2, -1, -1):
            G[t] = traj_r[t] + gamma * G[t + 1]
        for t in range(T):
            with torch.no_grad():
                qv = qnet(torch.tensor(s2f(traj_s[t], env), dtype=torch.float32))
            pred.append(float(qv[traj_a[t]]))
            actual.append(G[t])
    return np.array(pred), np.array(actual)

def test_optimal_comparison(env, qnet):
    """Тест 5: сравнение длины пути DQN с BFS-оптимумом."""
    opt_dist = bfs_distance(env)
    pairs = []
    for r in range(env.H):
        for c in range(env.W):
            if (r, c) in env.pits or (r, c) == env.goal: continue
            if not np.isfinite(opt_dist[r, c]): continue
            _, l, s = run_greedy(env, qnet, start=(r, c))
            if s:
                pairs.append((opt_dist[r, c], l))
    return np.array(pairs)


# ============================================================
# 5. ОБУЧЕНИЕ + ТЕСТЫ
# ============================================================
print("Обучение DQN...")
env = GridWorld()
qnet, rewards, lengths, q_hist = train_dqn(env)
print(f"Готово, {len(rewards)} эпизодов.\n")

# --- Тест 1 ---
succ1, ml, sl, mr, sr_std = test_basic(env, qnet)
print(f"ТЕСТ 1 (жадная политика из старта, 300 эпизодов):")
print(f"  Успешность:    {succ1*100:.1f}%")
print(f"  Длина пути:    {ml:.2f} ± {sl:.2f}")
print(f"  Награда:       {mr:+.2f} ± {sr_std:.2f}\n")

# --- Тест 2 ---
succ_grid, len_grid = test_all_starts(env, qnet)
valid = ~np.isnan(succ_grid)
print(f"ТЕСТ 2 (старт из произвольной клетки):")
print(f"  Покрытие:      {np.nansum(succ_grid)/valid.sum()*100:.1f}% клеток ведут к цели")
print(f"  Средняя длина: {np.nanmean(len_grid):.2f}\n")

# --- Тест 3 ---
slip_levels = [0.0, 0.05, 0.1, 0.2, 0.3, 0.5]
res3 = test_stochastic(qnet, slip_levels)
print(f"ТЕСТ 3 (стохастика — обучили на slip=0, тестируем разные):")
print(f"  {'slip':>6}  {'успех':>8}  {'длина':>8}  {'награда':>10}")
for sl, sr, ml_, mr_ in res3:
    print(f"  {sl:>6.2f}  {sr*100:>7.1f}%  {ml_:>8.2f}  {mr_:>+9.2f}")
print()

# --- Тест 4 ---
pred_q, actual_g = test_calibration(env, qnet)
err = pred_q - actual_g
print(f"ТЕСТ 4 (калибровка Q vs фактический return):")
print(f"  Точек:         {len(pred_q)}")
print(f"  Средняя ошибка Q-actual: {err.mean():+.3f}")
print(f"  Std ошибки:    {err.std():.3f}")
print(f"  Корреляция:    {np.corrcoef(pred_q, actual_g)[0,1]:.4f}\n")

# --- Тест 5 ---
pairs = test_optimal_comparison(env, qnet)
ratio = pairs[:, 1] / pairs[:, 0]
print(f"ТЕСТ 5 (DQN vs BFS-оптимум):")
print(f"  Оцениваемых клеток:        {len(pairs)}")
print(f"  Точно оптимальных путей:   {(ratio == 1.0).sum()}/{len(pairs)}")
print(f"  Среднее отношение DQN/опт: {ratio.mean():.3f}")


# ============================================================
# 6. ВИЗУАЛИЗАЦИЯ
# ============================================================

# ---- 6.1 Кривые обучения + Q в стартовой клетке ----
fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))
w = 30
def smooth(x):
    return np.convolve(x, np.ones(w)/w, mode='valid')

axes[0].plot(rewards, alpha=0.2, color='#1f77b4')
axes[0].plot(np.arange(w-1, len(rewards)), smooth(rewards), color='#1f77b4', lw=2)
axes[0].axhline(0, color='k', lw=0.5); axes[0].grid(alpha=0.3)
axes[0].set_xlabel('Эпизод'); axes[0].set_ylabel('Награда')
axes[0].set_title('Награда за эпизод')

axes[1].plot(lengths, alpha=0.2, color='#d62728')
axes[1].plot(np.arange(w-1, len(lengths)), smooth(lengths), color='#d62728', lw=2)
axes[1].set_yscale('log'); axes[1].grid(alpha=0.3, which='both')
axes[1].set_xlabel('Эпизод'); axes[1].set_ylabel('Шагов')
axes[1].set_title('Длина эпизода')

colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']
labels = ['↑', '→', '↓', '←']
for i in range(4):
    axes[2].plot(q_hist[:, i], label=labels[i], color=colors[i], lw=1.3)
axes[2].set_xlabel('Эпизод'); axes[2].set_ylabel('Q(s_старт, a)')
axes[2].set_title('Q-значения в стартовой клетке')
axes[2].legend(); axes[2].grid(alpha=0.3)
plt.tight_layout()
plt.savefig('dqn_training.png', dpi=110, bbox_inches='tight')
plt.close()

# ---- 6.2 Тест 2 — heatmap по стартам ----
fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))
for ax, data, title, cmap, vmax in [
    (axes[0], succ_grid, 'Тест 2: успешность из каждой клетки',  'RdYlGn', 1.0),
    (axes[1], len_grid,  'Тест 2: длина пути из каждой клетки',  'viridis_r', np.nanmax(len_grid))]:
    im = ax.imshow(data, cmap=cmap, vmin=0, vmax=vmax)
    for r in range(env.H):
        for c in range(env.W):
            if (r, c) in env.pits:
                ax.add_patch(Rectangle((c-0.5, r-0.5), 1, 1, facecolor='#444', alpha=1.0))
                ax.text(c, r, 'X', ha='center', va='center', color='white', fontweight='bold')
            elif (r, c) == env.goal:
                ax.add_patch(Rectangle((c-0.5, r-0.5), 1, 1, edgecolor='lime', facecolor='none', lw=3))
                ax.text(c, r, 'G', ha='center', va='center', color='black', fontweight='bold')
            elif np.isfinite(data[r, c]):
                v = data[r, c]
                txt = f"{int(v*100)}%" if vmax <= 1.0 else f"{int(v)}"
                ax.text(c, r, txt, ha='center', va='center',
                        color='white' if (vmax <= 1.0 and v < 0.5) or (vmax > 1.0 and v > vmax*0.6) else 'black',
                        fontsize=10)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(title)
    plt.colorbar(im, ax=ax, fraction=0.046)
plt.tight_layout()
plt.savefig('dqn_test_starts.png', dpi=110, bbox_inches='tight')
plt.close()

# ---- 6.3 Тест 3 — стохастика ----
fig, ax = plt.subplots(figsize=(9, 5))
sls = [r[0] for r in res3]
srs = [r[1] for r in res3]
mls = [r[2] for r in res3]
ax2 = ax.twinx()
b1 = ax.bar(np.array(sls)-0.012, srs, width=0.025, color='#2ca02c', alpha=0.7, label='Успех')
b2 = ax2.bar(np.array(sls)+0.012, mls, width=0.025, color='#1f77b4', alpha=0.7, label='Ср. длина')
ax.set_xlabel('Вероятность "скольжения" (slip)')
ax.set_ylabel('Доля успешных эпизодов', color='#2ca02c')
ax2.set_ylabel('Средняя длина пути', color='#1f77b4')
ax.set_title('Тест 3: робастность к стохастике\n(обучали на slip=0)')
ax.set_ylim(0, 1.05); ax.grid(alpha=0.3)
ax.legend(loc='upper left'); ax2.legend(loc='upper right')
plt.tight_layout()
plt.savefig('dqn_test_stochastic.png', dpi=110, bbox_inches='tight')
plt.close()

# ---- 6.4 Тест 4 — калибровка ----
fig, axes = plt.subplots(1, 2, figsize=(13, 5))
axes[0].scatter(actual_g, pred_q, s=18, alpha=0.4, color='#1f77b4')
mn = min(actual_g.min(), pred_q.min()); mx = max(actual_g.max(), pred_q.max())
axes[0].plot([mn, mx], [mn, mx], 'r--', lw=1, label='идеал y=x')
axes[0].set_xlabel('Фактический return G (по жадной траектории)')
axes[0].set_ylabel('Предсказанный Q(s, a)')
axes[0].set_title(f'Калибровка Q-функции\n(corr = {np.corrcoef(pred_q, actual_g)[0,1]:.3f})')
axes[0].grid(alpha=0.3); axes[0].legend()

axes[1].hist(err, bins=40, color='#ff7f0e', alpha=0.75)
axes[1].axvline(0, color='k', lw=0.8)
axes[1].axvline(err.mean(), color='red', lw=1.5, ls='--', label=f'mean = {err.mean():+.3f}')
axes[1].set_xlabel('Ошибка Q(s,a) - G')
axes[1].set_ylabel('Количество точек')
axes[1].set_title('Распределение ошибки')
axes[1].legend(); axes[1].grid(alpha=0.3)
plt.tight_layout()
plt.savefig('dqn_test_calibration.png', dpi=110, bbox_inches='tight')
plt.close()

print("\nГрафики сохранены: dqn_training.png, dqn_test_starts.png, "
      "dqn_test_stochastic.png, dqn_test_calibration.png")
