-- Recursive-CTE traversal over the generic `links` adjacency table.

CREATE OR REPLACE FUNCTION traverse_links(
    p_table     TEXT,
    p_id        UUID,
    p_max_depth INT DEFAULT 2,
    p_limit     INT DEFAULT 50
)
RETURNS TABLE (
    node_table TEXT,
    node_id    UUID,
    relationship TEXT,
    depth      INT
)
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE walk(node_table, node_id, relationship, depth, visited) AS (
        SELECT p_table, p_id, NULL::TEXT, 0, ARRAY[p_id]
        UNION ALL
        SELECT
            CASE WHEN l.source_table = walk.node_table AND l.source_id = walk.node_id
                 THEN l.target_table ELSE l.source_table END,
            CASE WHEN l.source_table = walk.node_table AND l.source_id = walk.node_id
                 THEN l.target_id ELSE l.source_id END,
            l.relationship,
            walk.depth + 1,
            walk.visited || (CASE WHEN l.source_table = walk.node_table AND l.source_id = walk.node_id
                 THEN l.target_id ELSE l.source_id END)
        FROM links l
        JOIN walk ON
            (l.source_table = walk.node_table AND l.source_id = walk.node_id)
            OR (l.target_table = walk.node_table AND l.target_id = walk.node_id)
        WHERE walk.depth < p_max_depth
          AND NOT (CASE WHEN l.source_table = walk.node_table AND l.source_id = walk.node_id
                   THEN l.target_id ELSE l.source_id END = ANY(walk.visited))
    )
    SELECT node_table, node_id, relationship, depth
    FROM walk
    WHERE depth > 0
    ORDER BY depth, node_table
    LIMIT p_limit;
$$;
