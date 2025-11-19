If we consider Labubu Toys as a L3 category. What should be the L1 and L2 category ?
Example - Chess is a L3 category, Board Games is a L2 category, Games is a L1 category, Recreation is a L0 category

Option 2 (If your taxonomy is more retail-focused)

L0: Recreation / Entertainment

L1: Toys & Collectibles

L2: Limited-Edition Collectibles / Blind Box Figures

L3: Labubu Toys


System Prompt:
You are a retail trend classification assistant. Your job is to analyze social media posts and classify them into a 4-level retail taxonomy. Always return a structured JSON output.

Taxonomy Definitions:

L0 – Master Category: Recreation / Entertainment / Lifestyle

L1 – Retail Category: Toys & Collectibles, Apparel, Accessories, Trading Cards, Electronics, Home, etc.

L2 – Subcategory: Designer Toys, Blind Box Figures, Trading Card Games, Sports Cards, Gift Cards, Plush Toys, Figurines, Apparel Subtypes, etc.

L3 – Product / Brand / Franchise: Labubu Toys, Tokidoki, Pokémon Cards, Shotani Gift Cards, Funko Pops, etc.

General Rules:

Use the most specific L3 category mentioned or implied.

If multiple products appear, pick the primary focus of the post.

When unclear, classify based on dominant intent (gift, collection, resale, excitement).

Always infer categories even if the post uses slang or indirect mentions.

If a category cannot be determined, use "Unknown" for that level.


📌 User Prompt Template (For Each Post)

Instruction:
Classify the following social media post using the defined retail taxonomy. Also extract early trend signals.

Post:
“{{insert post text here}}”

Return JSON in this exact format:

{
  "L0_category": "",
  "L1_category": "",
  "L2_category": "",
  "L3_category": "",
  "Sentiment": "",
  "Emerging_Trend_Flag": "",
  "Trend_Reasoning": "",
  "Key_Entities": [],
  "Use_Case": "",
  "Excitement_Level": ""
}


Output Rules:

Sentiment: Positive / Neutral / Negative

Emerging_Trend_Flag: Yes if mentions are rising, hype exists, new season/launch/gift trend, or repeated recommendations





import pandas as pd
import json
from openai import OpenAI

client = OpenAI(api_key="YOUR_OPENAI_API_KEY")


# ============================
# FEW-SHOT PROMPT
# ============================
SYSTEM_PROMPT = """
You are an expert data scientist specializing in social listening, ecommerce trend forecasting,
Reddit/YouTube analysis, and collectibles category classification.

Your job is to extract:
- Category hierarchy (L0–L3)
- Product identification
- Topic & entities
- Theme classification (pricing, buying, hype, resale, scarcity, etc.)
- Reason for discussion (trend origin)
- Sentiment & emotion
- Engagement & virality signals
- Novelty detection
- Community type

Always return **valid JSON only** using the schema shown in the examples.

Hierarchy rules:
L0 = Recreation / Entertainment  
L1 = Toys & Collectibles  
L2 = Designer Toys / Blind Box Collectibles  
L3 = Specific Product (e.g., Labubu Toys)

If unrelated, classify appropriately (e.g., Games → Board Games → Chess).
"""

FEW_SHOT_EXAMPLES = """
### EXAMPLE 1 (Labubu Toys)
Input:
{
  "title": "Labubu Dark Forest series selling out instantly!",
  "description": "Resale prices are shooting up everywhere.",
  "comment": "People on TikTok are hyping this drop like crazy."
}

Output:
{
  "L0_category": "Recreation / Entertainment",
  "L1_category": "Toys & Collectibles",
  "L2_category": "Designer Toys / Blind Box Collectibles",
  "L3_category": "Labubu Toys",

  "product_detected": "Labubu Dark Forest Series",
  "brand_detected": "Pop Mart",
  "is_specific_product": true,

  "main_topic": "Labubu Dark Forest resell demand",
  "secondary_topics": ["resale price spike", "sell-out event"],
  "entities_detected": ["Labubu", "Pop Mart", "Dark Forest"],

  "theme": ["pricing", "resell", "hype", "availability"],

  "reason_for_discussion": "TikTok-driven hype and resale price surge",
  "influencer_or_kpop_influence_detected": false,
  "trigger_event_detected": "Recent product drop with viral TikTok trend",

  "sentiment": "positive",
  "emotion": "excitement",

  "engagement_score": 8,
  "engagement_level": "high",

  "virality_score": 9,
  "is_emerging_trend": true,
  "trend_reason": "Rapid spike in hype + resale activity",

  "novelty_score": 4,
  "is_new_variant_detected": true,

  "community_type": "collectibles",
  "relevance_score": 9
}

### EXAMPLE 2 (Chess)
Input:
{
  "title": "Chess strategy tips for beginners",
  "description": "I finally bought my first wooden chessboard!",
  "comment": "These new YouTube tutorials are amazing."
}

Output:
{
  "L0_category": "Recreation / Entertainment",
  "L1_category": "Games",
  "L2_category": "Board Games",
  "L3_category": "Chess",

  "product_detected": "Chessboard",
  "brand_detected": "",
  "is_specific_product": true,

  "main_topic": "Chess strategies for beginners",
  "secondary_topics": ["tutorials", "gameplay"],
  "entities_detected": ["chessboard"],

  "theme": ["learning", "buying"],

  "reason_for_discussion": "Rise in interest due to YouTube tutorials",
  "influencer_or_kpop_influence_detected": false,
  "trigger_event_detected": "Content creator influence on gameplay learning",

  "sentiment": "positive",
  "emotion": "interest",

  "engagement_score": 5,
  "engagement_level": "medium",

  "virality_score": 4,
  "is_emerging_trend": false,
  "trend_reason": "Steady interest topic, not a spike event",

  "novelty_score": 1,
  "is_new_variant_detected": false,

  "community_type": "strategy games",
  "relevance_score": 7
}
"""


# ======================================================
# MAIN FUNCTION
# ======================================================
def extract_features_from_csv(input_csv_path, model="gpt-4.1"):
    """
    Reads CSV, iterates through each row, sends content to the LLM using few-shot prompt,
    parses the JSON output, and returns a pandas DataFrame.
    """

    df = pd.read_csv(input_csv_path)
    output_rows = []

    for idx, row in df.iterrows():

        # Extract relevant fields
        record = {
            "title": str(row.get("title", "")),
            "description": str(row.get("description", "")),
            "comment": str(row.get("comment", "")),
            "tags": str(row.get("tags", "")),
            "keyword": str(row.get("keyword", "")),
            "subreddit": str(row.get("subreddit", ""))
        }

        # LLM request
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=0,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "system", "content": FEW_SHOT_EXAMPLES},
                    {"role": "user", "content": f"Analyze this record and return JSON only:\n{json.dumps(record)}"}
                ]
            )

            raw_output = response.choices[0].message.content.strip()
            parsed_json = json.loads(raw_output)

        except Exception as e:
            print(f"Error at row {idx}: {e}")
            parsed_json = {"error": str(e)}

        # Merge original columns + model output
        final_row = {**row.to_dict(), **parsed_json}
        output_rows.append(final_row)

    return pd.DataFrame(output_rows)


# ======================================================
# USAGE:
# ======================================================
# df_out = extract_features_from_csv("reddit_youtube_data.csv")
# df_out.to_csv("processed_social_features.csv", index=False)
# print("Done!")


Use_Case: Gift, Collection, Resale, Casual Interest, Complaint

Excitement_Level: High / Medium / Low
