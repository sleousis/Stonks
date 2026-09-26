"""Unit tests for the visibility-graph helpers in features/visibility_graph."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.features.visibility_graph import (
    average_shortest_path_length,
    horizontal_visibility_graph,
    natural_visibility_graph,
    shortest_path_lengths,
)


def _edges(adj: np.ndarray) -> set[tuple[int, int]]:
    n = len(adj)
    return {(i, j) for i in range(n) for j in range(i + 1, n) if adj[i, j]}


FIVE = np.array([3.0, 1.0, 2.0, 1.0, 3.0])
FIVE_EDGES = {(0, 1), (1, 2), (2, 3), (3, 4), (0, 2), (0, 4), (2, 4)}


def test_natural_vg_on_known_five_point_series():
    # hand-checked: (0,2) sees over the dip, (0,4) over everything, (2,4)
    # over bar 3; (0,3), (1,3), (1,4) are blocked by bar 2.
    assert _edges(natural_visibility_graph(FIVE)) == FIVE_EDGES


def test_natural_vg_is_symmetric_with_empty_diagonal():
    adj = natural_visibility_graph(np.array([20, 40, 48, 70, 40, 60, 40, 100, 40, 80.0]))
    assert (adj == adj.T).all()
    assert not adj.diagonal().any()


def test_collinear_point_blocks_visibility():
    # the middle point sits exactly on the line of sight -> blocked
    assert _edges(natural_visibility_graph(np.array([1.0, 2.0, 3.0]))) == {(0, 1), (1, 2)}


def test_natural_vg_matches_brute_force_on_random_series():
    rng = np.random.default_rng(3)
    y = rng.normal(size=30).cumsum()
    adj = natural_visibility_graph(y)
    for a in range(len(y)):
        for b in range(a + 1, len(y)):
            visible = all(y[c] < y[b] + (y[a] - y[b]) * (b - c) / (b - a) for c in range(a + 1, b))
            assert bool(adj[a, b]) == visible, (a, b)


def test_horizontal_vg_is_subgraph_of_natural():
    y = np.array([0.0, 1.0, 3.0])
    # natural sees 0 -> 2 over the ramp; horizontal needs bar 1 below both ends
    assert (0, 2) in _edges(natural_visibility_graph(y))
    assert _edges(horizontal_visibility_graph(y)) == {(0, 1), (1, 2)}
    assert _edges(horizontal_visibility_graph(FIVE)) == FIVE_EDGES


def test_shortest_path_lengths_bfs():
    dist = shortest_path_lengths(natural_visibility_graph(FIVE))
    assert dist[1].tolist() == [1, 0, 1, 2, 2]
    assert dist[3].tolist() == [2, 2, 1, 0, 1]


def test_average_shortest_path_length_known_values():
    assert average_shortest_path_length(natural_visibility_graph(FIVE)) == pytest.approx(26 / 20)
    path3 = np.array([[0, 1, 0], [1, 0, 1], [0, 1, 0]], dtype=bool)
    assert average_shortest_path_length(path3) == pytest.approx(8 / 6)


def test_average_shortest_path_length_rejects_disconnected_graph():
    adj = np.zeros((3, 3), dtype=bool)
    adj[0, 1] = adj[1, 0] = True
    with pytest.raises(ValueError, match="connected"):
        average_shortest_path_length(adj)


def test_degenerate_sizes():
    assert natural_visibility_graph(np.array([])).shape == (0, 0)
    assert average_shortest_path_length(np.zeros((1, 1), dtype=bool)) == 0.0
