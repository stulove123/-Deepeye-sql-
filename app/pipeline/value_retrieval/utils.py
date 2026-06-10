from typing import Any, Dict, List, Optional, Tuple
from app.llm import LLM
from chromadb.types import Collection
from app.prompt import PromptFactory
from app.llm_extractor import LLMExtractor
import ast
import re
import json
import math
from collections import defaultdict
from app.logger import logger
from tenacity import(
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential
)
from openai import RateLimitError, APITimeoutError


STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "how", "in", "is", "it", "its", "list", "many", "of", "on",
    "or", "please", "show", "that", "the", "their", "there", "these", "this",
    "to", "what", "when", "where", "which", "who", "with",
}


def _append_unique(items: List[str], value: str, seen: set[str]) -> None:
    if value and value not in seen:
        seen.add(value)
        items.append(value)


def _post_process_keywords(keywords_list: List[str]) -> List[str]:
    processed_keywords: List[str] = []
    seen = set()
    for keyword in keywords_list:
        keyword = str(keyword).strip()
        _append_unique(processed_keywords, keyword, seen)
        for keyword_part in keyword.split(" "):
            _append_unique(processed_keywords, keyword_part.strip(), seen)
    return processed_keywords


def filter_retrieval_keywords(
    keywords: List[str],
    *,
    drop_stopwords: bool = False,
    min_keyword_length: int = 1,
) -> List[str]:
    filtered_keywords: List[str] = []
    seen = set()
    for keyword in keywords:
        normalized_keyword = str(keyword).strip()
        if len(normalized_keyword) < min_keyword_length:
            continue
        if drop_stopwords and normalized_keyword.lower() in STOPWORDS:
            continue
        _append_unique(filtered_keywords, normalized_keyword, seen)
    return filtered_keywords


def _parse_keywords_response(response: str) -> Optional[List[str]]:
    """Parse keywords from LLM response."""
    try:
        match = re.search(r"<result>(.*?)</result>", response, re.DOTALL)
        if match is None:
            return None

        raw_list = match.group(1).strip()
        try:
            keywords_list = json.loads(raw_list)
        except json.JSONDecodeError:
            keywords_list = ast.literal_eval(raw_list)

        if isinstance(keywords_list, list):
            return [str(keyword) for keyword in keywords_list]
        return None
    except Exception as e:
        logger.debug(f"Error parsing keywords: {e}")
        return None


def extract_keywords(
    question: str,
    evidence: str,
    llm: LLM,
    fix_end_token: bool = False,
    extractor_max_retry: Optional[int] = None,
    extractor: Optional[LLMExtractor] = None,
) -> tuple[List[str], Dict[str, int]]:
    prompt = PromptFactory.format_keywords_extraction_prompt(question, evidence)

    if extractor is None:
        extractor = LLMExtractor() if extractor_max_retry is None else LLMExtractor(max_retry=extractor_max_retry)
    results, total_token_usage = extractor.extract_with_retry(
        llm=llm,
        messages=[{"role": "user", "content": prompt}],
        rule_parser=_parse_keywords_response,
        fix_end_token=fix_end_token,
        end_token="</result>",
        n=1,
    )

    if results:
        keywords_list = results[0]
    else:
        logger.warning("Failed to extract keywords from LLM response, using default keywords splitting strategy")
        keywords_list = question.split(" ") + evidence.split(" ")

    keywords_list = _post_process_keywords(keywords_list)

    return keywords_list, total_token_usage


@retry(
    wait=wait_random_exponential(multiplier=1, max=60),
    stop=stop_after_attempt(10),
    retry=retry_if_exception_type((RateLimitError, APITimeoutError))
)
def embed_keywords(keywords: List[str], embedding_function: Any, batch_size: int) -> List[List[float]]:
    """
    Independently embed keywords with batching and retry logic.
    """
    if not keywords:
        return []

    all_embeddings = []
    
    # Manual batching to respect API limits (e.g., max 10 per request)
    for i in range(0, len(keywords), batch_size):
        batch = keywords[i : i + batch_size]
        batch_embeddings = embedding_function(batch)
        all_embeddings.extend(batch_embeddings)
        
    return all_embeddings


def retrieve_values_for_one_column(
    query_embeddings: List[List[float]], # Changed from keywords: List[str]
    collection: Collection,
    table_name: str,
    column_name: str,
    n_results: int,
    lower_meta_data: bool
) -> Dict[str, Any]:
    result = retrieve_candidates_for_one_column(
        keywords=[],
        query_embeddings=query_embeddings,
        collection=collection,
        table_name=table_name,
        column_name=column_name,
        n_results=n_results,
        lower_meta_data=lower_meta_data,
    )

    values = []
    for candidate in result["candidates"]:
        values.append((candidate["value"], candidate["value_distance"]))

    seen_values = set()
    top_k_values = []
    for value, distance in sorted(values, key=lambda x: x[1]):
        if value not in seen_values:
            seen_values.add(value)
            top_k_values.append({"value": value, "distance": distance})
            if len(top_k_values) >= n_results:
                break

    return {
        "table_name": result["table_name"],
        "column_name": result["column_name"],
        "values": top_k_values,
    }


def retrieve_candidates_for_one_column(
    keywords: List[str],
    query_embeddings: List[List[float]],
    collection: Collection,
    table_name: str,
    column_name: str,
    n_results: int,
    lower_meta_data: bool,
) -> Dict[str, Any]:
    table_name = table_name.lower() if lower_meta_data else table_name
    column_name = column_name.lower() if lower_meta_data else column_name

    if not query_embeddings:
        return {
            "table_name": table_name,
            "column_name": column_name,
            "candidates": [],
        }
    
    # We no longer need batching here because we already have the embeddings
    query_results = collection.query(
        query_embeddings=query_embeddings, # Pass pre-computed embeddings
        where={"$and": [{"table_name": {"$eq": table_name}}, {"column_name": {"$eq": column_name}}]},
        n_results=n_results,
    )
    
    candidates = []
    for keyword_idx, (documents, distances) in enumerate(zip(query_results["documents"], query_results["distances"])):
        keyword = keywords[keyword_idx] if keyword_idx < len(keywords) else ""
        for doc, dist in zip(documents, distances):
            value_distance = float(dist)
            candidates.append(
                {
                    "keyword": keyword,
                    "keyword_idx": keyword_idx,
                    "table_name": table_name,
                    "column_name": column_name,
                    "value": doc,
                    "value_distance": value_distance,
                    "value_similarity": 1.0 - value_distance,
                    "final_score": 1.0 - value_distance,
                }
            )

    return {
        "table_name": table_name,
        "column_name": column_name,
        "candidates": candidates,
    }


def build_schema_context_texts(
    database_schema: Dict[str, Any],
    column_tasks: List[Tuple[str, str]],
    lower_meta_data: bool = False,
) -> tuple[Dict[str, str], Dict[Tuple[str, str], str]]:
    table_contexts: Dict[str, str] = {}
    column_contexts: Dict[Tuple[str, str], str] = {}
    for table_name, column_name in column_tasks:
        lookup_table_name = table_name.lower() if lower_meta_data else table_name
        lookup_column_name = column_name.lower() if lower_meta_data else column_name
        table_schema = database_schema["tables"].get(table_name, {})
        column_schema = table_schema.get("columns", {}).get(column_name, {})

        if lookup_table_name not in table_contexts:
            table_parts = [
                "table",
                table_schema.get("table_name") or table_name,
                table_schema.get("table_fullname") or "",
                table_schema.get("description") or "",
            ]
            table_contexts[lookup_table_name] = " ".join(str(part) for part in table_parts if part)

        column_parts = [
            "table",
            table_schema.get("table_name") or table_name,
            "column",
            column_schema.get("column_name") or column_name,
            "type",
            column_schema.get("column_type") or "",
            column_schema.get("description") or "",
        ]
        column_contexts[(lookup_table_name, lookup_column_name)] = " ".join(str(part) for part in column_parts if part)
    return table_contexts, column_contexts


def embed_schema_contexts(
    database_schema: Dict[str, Any],
    column_tasks: List[Tuple[str, str]],
    embedding_function: Any,
    batch_size: int,
    lower_meta_data: bool = False,
) -> tuple[Dict[str, List[float]], Dict[Tuple[str, str], List[float]]]:
    table_contexts, column_contexts = build_schema_context_texts(
        database_schema,
        column_tasks,
        lower_meta_data=lower_meta_data,
    )
    context_items: List[Tuple[str, Any, str]] = []
    for table_name, context_text in table_contexts.items():
        context_items.append(("table", table_name, context_text))
    for column_key, context_text in column_contexts.items():
        context_items.append(("column", column_key, context_text))

    if not context_items:
        return {}, {}

    embeddings = embed_keywords([context_text for _, _, context_text in context_items], embedding_function, batch_size)
    table_embeddings: Dict[str, List[float]] = {}
    column_embeddings: Dict[Tuple[str, str], List[float]] = {}
    for (context_type, context_key, _), embedding in zip(context_items, embeddings):
        if context_type == "table":
            table_embeddings[context_key] = embedding
        else:
            column_embeddings[context_key] = embedding
    return table_embeddings, column_embeddings


def _is_empty_vector(vector: Any) -> bool:
    if vector is None:
        return True
    try:
        return len(vector) == 0
    except TypeError:
        return True


def _cosine_similarity(vector_a: List[float], vector_b: List[float]) -> float:
    if _is_empty_vector(vector_a) or _is_empty_vector(vector_b):
        return 0.0
    dim = min(len(vector_a), len(vector_b))
    if dim == 0:
        return 0.0
    dot_product = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for idx in range(dim):
        a_value = float(vector_a[idx])
        b_value = float(vector_b[idx])
        dot_product += a_value * b_value
        norm_a += a_value * a_value
        norm_b += b_value * b_value
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot_product / math.sqrt(norm_a * norm_b)


def _sigmoid(value: float) -> float:
    if value >= 60:
        return 1.0
    if value <= -60:
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))


def rerank_candidates_with_context(
    candidates: List[Dict[str, Any]],
    query_embeddings: List[List[float]],
    table_context_embeddings: Dict[str, List[float]],
    column_context_embeddings: Dict[Tuple[str, str], List[float]],
    *,
    value_similarity_weight: float,
    column_similarity_weight: float,
    table_similarity_weight: float,
    context_similarity_threshold: float,
    context_similarity_slope: float,
    value_rescue_threshold: float,
    value_rescue_slope: float,
    context_penalty_floor: float,
) -> List[Dict[str, Any]]:
    reranked_candidates = []
    context_weight_sum = column_similarity_weight + table_similarity_weight

    for candidate in candidates:
        keyword_idx = int(candidate.get("keyword_idx", -1))
        query_embedding = query_embeddings[keyword_idx] if 0 <= keyword_idx < len(query_embeddings) else []
        table_name = candidate["table_name"]
        column_name = candidate["column_name"]

        column_similarity = _cosine_similarity(
            query_embedding,
            column_context_embeddings.get((table_name, column_name), []),
        )
        table_similarity = _cosine_similarity(
            query_embedding,
            table_context_embeddings.get(table_name, []),
        )

        if context_weight_sum > 0:
            context_similarity = (
                column_similarity_weight * column_similarity
                + table_similarity_weight * table_similarity
            ) / context_weight_sum
        else:
            context_similarity = 1.0

        value_similarity = float(candidate.get("value_similarity", 0.0))
        context_gate = _sigmoid(context_similarity_slope * (context_similarity - context_similarity_threshold))
        rescue_gate = _sigmoid(value_rescue_slope * (value_similarity - value_rescue_threshold))
        context_multiplier = max(context_penalty_floor, context_gate)
        final_score = value_similarity_weight * value_similarity * (
            rescue_gate + (1.0 - rescue_gate) * context_multiplier
        )

        updated_candidate = dict(candidate)
        updated_candidate.update(
            {
                "column_similarity": column_similarity,
                "table_similarity": table_similarity,
                "context_similarity": context_similarity,
                "context_gate": context_gate,
                "value_rescue_gate": rescue_gate,
                "final_score": final_score,
            }
        )
        reranked_candidates.append(updated_candidate)

    return reranked_candidates


def _candidate_sort_key(candidate: Dict[str, Any]) -> tuple[float, float]:
    return (
        float(candidate.get("final_score", candidate.get("value_similarity", 0.0))),
        float(candidate.get("value_similarity", 0.0)),
    )


def _merge_selected_candidate(
    selected_candidates: Dict[Tuple[str, str, str], Dict[str, Any]],
    candidate: Dict[str, Any],
) -> None:
    candidate_key = (candidate["table_name"], candidate["column_name"], str(candidate["value"]))
    existing_candidate = selected_candidates.get(candidate_key)
    keyword = candidate.get("keyword", "")
    if existing_candidate is None:
        selected_candidate = dict(candidate)
        selected_candidate["matched_keywords"] = [keyword] if keyword else []
        selected_candidates[candidate_key] = selected_candidate
        return

    if keyword and keyword not in existing_candidate.setdefault("matched_keywords", []):
        existing_candidate["matched_keywords"].append(keyword)

    if _candidate_sort_key(candidate) > _candidate_sort_key(existing_candidate):
        matched_keywords = existing_candidate.get("matched_keywords", [])
        selected_candidate = dict(candidate)
        selected_candidate["matched_keywords"] = matched_keywords
        selected_candidates[candidate_key] = selected_candidate


def select_candidates_per_column(
    candidates: List[Dict[str, Any]],
    *,
    max_values_per_column: int,
    score_threshold: float,
) -> List[Dict[str, Any]]:
    selected_candidates: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    per_column_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    for candidate in sorted(candidates, key=_candidate_sort_key, reverse=True):
        if float(candidate.get("final_score", 0.0)) < score_threshold:
            continue
        column_key = (candidate["table_name"], candidate["column_name"])
        if per_column_counts[column_key] >= max_values_per_column:
            continue
        candidate_key = (candidate["table_name"], candidate["column_name"], str(candidate["value"]))
        is_new_value = candidate_key not in selected_candidates
        _merge_selected_candidate(selected_candidates, candidate)
        if is_new_value:
            per_column_counts[column_key] += 1
    return sorted(selected_candidates.values(), key=_candidate_sort_key, reverse=True)


def select_global_candidates_with_quota(
    candidates: List[Dict[str, Any]],
    *,
    global_top_k_per_keyword: int,
    per_column_quota_per_keyword: int,
    max_values_per_column: int,
    score_threshold: float,
) -> List[Dict[str, Any]]:
    candidates_by_keyword: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_keyword[int(candidate.get("keyword_idx", -1))].append(candidate)

    selected_candidates: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    final_column_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    for keyword_idx in sorted(candidates_by_keyword):
        keyword_candidates = sorted(candidates_by_keyword[keyword_idx], key=_candidate_sort_key, reverse=True)
        per_keyword_column_counts: Dict[Tuple[str, str], int] = defaultdict(int)
        selected_for_keyword = 0
        for candidate in keyword_candidates:
            if selected_for_keyword >= global_top_k_per_keyword:
                break
            if float(candidate.get("final_score", 0.0)) < score_threshold:
                continue
            column_key = (candidate["table_name"], candidate["column_name"])
            if per_keyword_column_counts[column_key] >= per_column_quota_per_keyword:
                continue
            if final_column_counts[column_key] >= max_values_per_column:
                continue

            candidate_key = (candidate["table_name"], candidate["column_name"], str(candidate["value"]))
            is_new_value = candidate_key not in selected_candidates
            _merge_selected_candidate(selected_candidates, candidate)
            if is_new_value:
                per_keyword_column_counts[column_key] += 1
                final_column_counts[column_key] += 1
                selected_for_keyword += 1

    return sorted(selected_candidates.values(), key=_candidate_sort_key, reverse=True)


def group_selected_candidates_by_column(
    candidates: List[Dict[str, Any]],
    *,
    max_values_per_column: int,
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    grouped_candidates: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    seen_values = set()
    for candidate in sorted(candidates, key=_candidate_sort_key, reverse=True):
        table_name = candidate["table_name"]
        column_name = candidate["column_name"]
        value = candidate["value"]
        candidate_key = (table_name, column_name, str(value))
        if candidate_key in seen_values:
            continue
        if len(grouped_candidates[table_name][column_name]) >= max_values_per_column:
            continue
        seen_values.add(candidate_key)
        final_score = float(candidate.get("final_score", candidate.get("value_similarity", 0.0)))
        value_distance = candidate.get("value_distance")
        if value_distance is None:
            value_distance = max(0.0, 1.0 - float(candidate.get("value_similarity", final_score)))
        grouped_candidates[table_name][column_name].append(
            {
                "value": value,
                "distance": value_distance,
                "score": final_score,
                "rerank_distance": max(0.0, 1.0 - final_score),
                "value_distance": value_distance,
                "value_similarity": candidate.get("value_similarity"),
                "column_similarity": candidate.get("column_similarity"),
                "table_similarity": candidate.get("table_similarity"),
                "keyword": candidate.get("keyword"),
                "matched_keywords": candidate.get("matched_keywords", []),
            }
        )
    return {
        table_name: dict(columns)
        for table_name, columns in grouped_candidates.items()
    }
