"""Context-aware decision engine based on dialogue state and question type."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .models import Candidate


class ContextAwareDecisionEngine:
    """
    Smart decision engine that considers dialogue context and question type.
    
    Key insight: User response patterns depend heavily on the assistant's
    previous question type:
    - Yes/no question ("...吗？") → high confidence for affirmatives/negatives
    - Choice question ("A还是B？") → high confidence for A/B mentions
    - Statement ("...。") → lower confidence, may ask follow-up
    """
    
    def __init__(self):
        # Simple affirmatives (without punctuation for flexibility)
        self.affirmatives = {
            "是", "是的", "对", "对的", "嗯", "好", "好的", "可以", "行",
            "没错", "确实", "就是", "正是", "明白", "明白了", "懂了", "知道了",
            "了解", "清楚", "清楚了", "收到", "嗯嗯", "好吧", "行吧"
        }
        
        # Simple negatives
        self.negatives = {
            "不是", "不对", "不", "没", "没有", "不行", "不可以", "不用",
            "不要", "算了", "不了", "不需要"
        }
        
        # Question keywords (indicates user asking new question)
        self.question_keywords = {
            "什么", "怎么", "为什么", "哪", "多少", "几", "如何", "吗", "？", "?"
        }
    
    def should_reuse(
        self,
        user_input: str,
        candidates: list,
        assistant_last_text: str = "",
    ) -> tuple[Literal["reuse", "say", "llm"], any]:
        """
        Context-aware decision considering assistant's previous question type.
        
        Args:
            user_input: User's current input
            candidates: Available prediction candidates
            assistant_last_text: Assistant's last utterance (for context)
        
        Returns:
            - ("reuse", candidate): confident reuse
            - ("say", None): confident rejection
            - ("llm", None): not confident, fallback to LLM
        """
        if not candidates or not user_input:
            return ("say", None)
        
        user_input = user_input.strip()
        user_clean = self._normalize(user_input)
        
        # Analyze assistant's last question type
        question_type = self._detect_question_type(assistant_last_text)
        
        # Rule 1: User asks new question → generate new
        if self._contains_question(user_input):
            return ("say", None)
        
        # Rule 2: Yes/no question + affirmative/negative → high confidence reuse
        if question_type == "yes_no":
            if user_clean in self.affirmatives:
                return ("reuse", candidates[0])
            elif user_clean in self.negatives:
                # Try to find a candidate explicitly for negative scenario
                for candidate in candidates:
                    if self._mentions_negative_scenario(candidate.when):
                        return ("reuse", candidate)
                # No explicit negative candidate, but user clearly rejected
                # Still try the second candidate if available (often covers rejection)
                if len(candidates) > 1:
                    return ("reuse", candidates[1])
                return ("say", None)
        
        # Rule 3: Choice question + option mention → high confidence reuse
        if question_type == "choice":
            choice_options = self._extract_choice_options(assistant_last_text)
            
            # Try exact keyword matching first
            for i, option in enumerate(choice_options):
                if self._fuzzy_contains(user_input, option):
                    # Match user response to the corresponding candidate
                    if i < len(candidates):
                        return ("reuse", candidates[i])
            
            # Try semantic matching with candidate conditions
            for candidate in candidates:
                for option in choice_options:
                    if self._fuzzy_contains(candidate.when, option) and \
                       self._fuzzy_contains(user_input, option):
                        return ("reuse", candidate)
            
            # User answered with generic affirmative (e.g., "都行", "随便", "好的")
            if user_clean in self.affirmatives:
                return ("reuse", candidates[0])
        
        # Rule 4: Statement + short affirmative → medium confidence
        if question_type == "statement":
            if user_clean in self.affirmatives and len(user_input) <= 5:
                # Statement context makes this less confident
                # Only reuse if candidate explicitly covers confirmation
                for candidate in candidates:
                    if self._mentions_confirmation(candidate.when):
                        return ("reuse", candidate)
                return ("llm", None)  # Fallback to LLM for safety
        
        # Rule 5: Simple negatives → likely need new response
        if user_clean in self.negatives:
            return ("say", None)
        
        # Rule 6: Keyword matching (original fuzzy logic)
        for candidate in candidates:
            if self._keyword_match(user_input, candidate.when):
                return ("reuse", candidate)
        
        # Not confident → fallback to LLM
        return ("llm", None)
    
    def _detect_question_type(self, text: str) -> Literal["yes_no", "choice", "statement", "wh"]:
        """Detect question type from assistant's last utterance."""
        if not text:
            return "statement"
        
        text = text.strip()
        
        # Yes/no question: "...吗？" or "...吗？"
        if re.search(r'[吗呢啊]？?$', text):
            return "yes_no"
        
        # Choice question: "A，还是B？" or "A还是B？"
        if re.search(r'[，,]?还是.+？', text) or re.search(r'或者.+？', text):
            return "choice"
        
        # Wh-question: "什么/怎么/为什么..."
        if any(kw in text for kw in ["什么", "怎么", "为什么", "哪", "如何"]):
            return "wh"
        
        # Default to statement
        return "statement"
    
    def _extract_choice_options(self, text: str) -> list[str]:
        """Extract options from choice question like '...A，还是B？'."""
        # Pattern: "...A，还是B？" or "...A还是B？"
        match = re.search(r'(.+?)[，,]?还是(.+?)？', text)
        if match:
            option_a = match.group(1).strip().split('。')[-1].split('，')[-1]
            option_b = match.group(2).strip()
            return [option_a, option_b]
        
        # Pattern: "...A或者B？"
        match = re.search(r'(.+?)或者(.+?)？', text)
        if match:
            option_a = match.group(1).strip().split('。')[-1].split('，')[-1]
            option_b = match.group(2).strip()
            return [option_a, option_b]
        
        return []
    
    def _mentions_negative_scenario(self, text: str) -> bool:
        """Check if candidate condition mentions negative response."""
        negative_markers = ["不是", "拒绝", "否定", "不同意", "不要", "没有"]
        return any(marker in text for marker in negative_markers)
    
    def _mentions_confirmation(self, text: str) -> bool:
        """Check if candidate condition mentions confirmation/acknowledgment."""
        confirmation_markers = ["确认", "明白", "理解", "同意", "接受", "承接"]
        return any(marker in text for marker in confirmation_markers)
    
    def _contains_question(self, text: str) -> bool:
        """Check if user input contains a question."""
        return any(keyword in text for keyword in self.question_keywords)
    
    def _normalize(self, text: str) -> str:
        """Remove punctuation for flexible matching."""
        return re.sub(r'[，。、；：？！""''（）【】\s]', '', text)
    
    def _fuzzy_contains(self, text: str, keyword: str) -> bool:
        """Check if text contains keyword (normalized)."""
        text_norm = self._normalize(text)
        keyword_norm = self._normalize(keyword)
        return keyword_norm in text_norm
    
    def _keyword_match(self, user_input: str, condition: str) -> bool:
        """Check if user input contains key entities from condition."""
        # Extract named entities (company names, brand names, etc.)
        entities = self._extract_entities(condition)
        if not entities:
            return False
        
        # Check if user input mentions any entity
        return any(self._fuzzy_contains(user_input, entity) for entity in entities)
    
    def _extract_entities(self, text: str) -> list[str]:
        """Extract potential named entities (simple heuristic)."""
        entities = []
        
        # Pattern 1: Quoted text (e.g., "青禾品牌")
        quoted = re.findall(r'[「『""]([^」』""]+)[」』""]', text)
        entities.extend(quoted)
        
        # Pattern 2: Specific company/brand keywords
        company_pattern = r'([一-龥]{2,6}(?:公司|品牌|商贸|商行))'
        companies = re.findall(company_pattern, text)
        entities.extend(companies)
        
        return entities
