"""Rule-based decision engine to reduce LLM calls in prediction reuse decisions."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .models import Candidate


class RuleBasedDecisionEngine:
    """Fast pattern-based decision engine that avoids LLM calls for obvious reuse cases."""
    
    def __init__(self):
        # Simple affirmative patterns
        self.affirmatives = {
            "是", "是的", "对", "对的", "嗯", "好", "好的", "可以", "行",
            "没错", "确实", "就是", "正是"
        }
        
        # Simple negative patterns
        self.negatives = {
            "不是", "不对", "不", "没", "没有", "不行", "不可以", "不用"
        }
        
        # Question keywords
        self.question_keywords = {
            "什么", "怎么", "为什么", "哪", "多少", "几", "如何", "吗", "？", "?"
        }
    
    def should_reuse(
        self,
        user_input: str,
        candidates: list,
    ) -> tuple[Literal["reuse", "say", "llm"], any]:
        """
        Decide whether to reuse a candidate WITHOUT calling LLM.
        
        Returns:
            - ("reuse", candidate): confident reuse decision
            - ("say", None): confident rejection, generate new
            - ("llm", None): not confident, fall back to LLM
        """
        if not candidates or not user_input:
            return ("say", None)
        
        user_input = user_input.strip()
        
        # Rule 1: Very short affirmatives (≤3 chars) → check candidates
        if len(user_input) <= 3 and user_input in self.affirmatives:
            for candidate in candidates:
                # If the candidate asks a yes/no question, reuse it
                if self._is_yes_no_question(candidate.when):
                    return ("reuse", candidate)
            return ("say", None)
        
        # Rule 2: Very short negatives → likely need new response
        if len(user_input) <= 3 and user_input in self.negatives:
            return ("say", None)
        
        # Rule 3: User asks a new question → generate new
        if self._contains_question(user_input):
            return ("say", None)
        
        # Rule 4: Exact or near-exact match with candidate condition
        for candidate in candidates:
            similarity = self._fuzzy_match(user_input, candidate.when)
            if similarity > 0.8:  # 80% similarity threshold
                return ("reuse", candidate)
        
        # Rule 5: Keyword-based matching
        for candidate in candidates:
            if self._keyword_match(user_input, candidate.when):
                return ("reuse", candidate)
        
        # Not confident enough → fall back to LLM
        return ("llm", None)
    
    def _is_yes_no_question(self, text: str) -> bool:
        """Check if text describes a yes/no question scenario."""
        yes_no_markers = ["吗", "还是", "是否", "或者", "要不要"]
        return any(marker in text for marker in yes_no_markers)
    
    def _contains_question(self, text: str) -> bool:
        """Check if user input contains a question."""
        return any(keyword in text for keyword in self.question_keywords)
    
    def _fuzzy_match(self, user_input: str, condition: str) -> float:
        """Calculate fuzzy match score between user input and candidate condition."""
        # Extract key phrases from condition (remove common words)
        condition_clean = self._clean_text(condition)
        user_clean = self._clean_text(user_input)
        
        if not condition_clean or not user_clean:
            return 0.0
        
        # Simple character-level overlap
        common = set(condition_clean) & set(user_clean)
        union = set(condition_clean) | set(user_clean)
        
        return len(common) / len(union) if union else 0.0
    
    def _keyword_match(self, user_input: str, condition: str) -> bool:
        """Check if user input contains key entities from condition."""
        # Extract named entities (company names, brand names, etc.)
        entities = self._extract_entities(condition)
        if not entities:
            return False
        
        # Check if user input mentions any entity
        return any(entity in user_input for entity in entities)
    
    def _clean_text(self, text: str) -> str:
        """Remove common filler words and punctuation."""
        stopwords = {"的", "了", "是", "在", "有", "和", "就", "都", "而", "及", "与"}
        # Remove punctuation
        text = re.sub(r'[，。、；：？！""''（）【】]', '', text)
        # Remove stopwords
        chars = [c for c in text if c not in stopwords]
        return ''.join(chars)
    
    def _extract_entities(self, text: str) -> list[str]:
        """Extract potential named entities (simple heuristic)."""
        # Look for capitalized words or quoted phrases
        entities = []
        
        # Pattern 1: Quoted text (e.g., "青禾品牌")
        quoted = re.findall(r'[「『""]([^」』""]+)[」』""]', text)
        entities.extend(quoted)
        
        # Pattern 2: Specific company/brand keywords
        company_pattern = r'([一-龥]{2,6}(?:公司|品牌|商贸|商行))'
        companies = re.findall(company_pattern, text)
        entities.extend(companies)
        
        return entities
