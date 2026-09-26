"""Time-series visibility graphs and their average shortest path length.

Source: neurotrader888/TimeSeriesVisibilityGraphs (MIT License, (c) 2023
neurotrader888). Stonks' own implementation of the published algorithm
(Lacasa et al. 2008, "From time series to complex networks: the visibility
graph"); no code is copied.

Each bar is a node. In the *natural* visibility graph bars ``a < b`` are
linked when every bar ``c`` between them lies strictly below the straight
line from ``(a, y_a)`` to ``(b, y_b)``. In the *horizontal* graph every bar
between them must lie strictly below ``min(y_a, y_b)``. Adjacent bars always
see each other, so both graphs are connected.

Deliberate deviations from the original:

- The original builds graphs with ``ts2vg`` and measures them with
  ``networkx``; we use no graph library. Edges come from an ``O(n^2)``
  sweep (a bar ``b`` is visible from ``a`` exactly when the slope ``a->b``
  beats every earlier slope ``a->c``) and path lengths from BFS.
- Time is the bar index (evenly spaced); the original accepts arbitrary
  timestamps but the strategy never uses them.
"""

from __future__ import annotations

from collections import deque

import numpy as np

__all__ = [
    "average_shortest_path_length",
    "horizontal_visibility_graph",
    "natural_visibility_graph",
    "shortest_path_lengths",
]


def natural_visibility_graph(values: np.ndarray) -> np.ndarray:
    """Boolean, symmetric adjacency matrix of the natural visibility graph.

    Ties block: a bar lying exactly on the line of sight hides the far end.
    """
    y = np.asarray(values, dtype=float)
    n = len(y)
    adj = np.zeros((n, n), dtype=bool)
    for a in range(n - 1):
        best = -np.inf  # steepest slope seen so far from ``a``
        ya = y[a]
        for b in range(a + 1, n):
            slope = (y[b] - ya) / (b - a)
            if slope > best:
                adj[a, b] = adj[b, a] = True
                best = slope
    return adj


def horizontal_visibility_graph(values: np.ndarray) -> np.ndarray:
    """Boolean, symmetric adjacency matrix of the horizontal visibility
    graph (bars in between must be strictly below both ends)."""
    y = np.asarray(values, dtype=float)
    n = len(y)
    adj = np.zeros((n, n), dtype=bool)
    for a in range(n - 1):
        highest_between = -np.inf
        for b in range(a + 1, n):
            if highest_between < min(y[a], y[b]):
                adj[a, b] = adj[b, a] = True
            highest_between = max(highest_between, y[b])
            if highest_between >= y[a]:
                break  # nothing further right can see ``a``
    return adj


def shortest_path_lengths(adj: np.ndarray) -> np.ndarray:
    """All-pairs hop counts via BFS from every node; ``-1`` marks an
    unreachable pair."""
    adj = np.asarray(adj, dtype=bool)
    n = len(adj)
    neighbours = [np.flatnonzero(adj[i]) for i in range(n)]
    dist = np.full((n, n), -1, dtype=int)
    for source in range(n):
        row = dist[source]
        row[source] = 0
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for nxt in neighbours[node]:
                if row[nxt] < 0:
                    row[nxt] = row[node] + 1
                    queue.append(nxt)
    return dist


def average_shortest_path_length(adj: np.ndarray) -> float:
    """Mean hop count over all ordered pairs of distinct nodes (the
    ``networkx`` definition). Raises ``ValueError`` on a disconnected graph."""
    n = len(adj)
    if n <= 1:
        return 0.0
    dist = shortest_path_lengths(adj)
    if (dist < 0).any():
        raise ValueError("average shortest path length needs a connected graph")
    return float(dist.sum()) / (n * (n - 1))
