# Troubleshooting Guide - Inflation-Busting Recipe Generator

## Recipe Not Generating (Showing "No recommendations yet")

### Root Causes & Solutions

#### 1. **Supabase Connection Issues**
- **Problem**: Database not connected or no ingredients loaded
- **Solution**:
  ```bash
  python -c "from chatbot import get_all_ingredients; print(len(get_all_ingredients()))"
  ```
  If this returns 0, the database needs to be seeded.

#### 2. **Database Not Seeded**
- **Problem**: The `offers` table is empty
- **Solution**: Run the data ingestion script
  ```bash
  python -c "from data_ingestion.load_to_supabase import upsert_offers; from data_ingestion.fetch_open_food_facts import fetch_off_discounts; offers = fetch_off_discounts(products_per_category=10); print(f'Loaded {upsert_offers(offers)} offers')"
  ```

#### 3. **LLM API Key Missing**
- **Problem**: `GROQ_API_KEY` not set in `.env`
- **Solution**: Add to `.env`:
  ```
  GROQ_API_KEY=your_groq_api_key_here
  ```

#### 4. **Session State Not Initializing**
- **Solution**: 
  - Click the **Refresh** button next to "Recipe Recommendations"
  - Or restart the Streamlit app:
    ```bash
    streamlit run app.py
    ```

### Verification Checklist

- [ ] Supabase URL and Key are set in `.env`
- [ ] GROQ_API_KEY is set in `.env`
- [ ] Database has ingredients:
  ```bash
  python -c "from config import supabase; print(supabase.table('offers').select('count').execute().count)"
  ```
- [ ] LLM works:
  ```bash
  python -c "from config import get_llm; llm = get_llm(); print('✓ LLM OK')"
  ```
- [ ] Ingredients can be fetched:
  ```bash
  python -c "from chatbot import get_all_ingredients; print(f'{len(get_all_ingredients())} ingredients loaded')"
  ```
- [ ] Recommendations generate:
  ```bash
  python -c "from chatbot import get_all_ingredients, generate_recommendations_with_reasons; recs = generate_recommendations_with_reasons('Veg', get_all_ingredients(), count=3); print(f'✓ {len(recs)} recipes generated')"
  ```

## Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `ModuleNotFoundError: No module named 'langchain_groq'` | Package not installed | `pip install langchain-groq` |
| `RuntimeError: Groq API key missing` | GROQ_API_KEY not in `.env` | Add `GROQ_API_KEY=...` to `.env` |
| `Supabase connection failed` | Invalid credentials | Check SUPABASE_URL and SUPABASE_KEY |
| `No recommendations yet` | Database empty or session not initialized | Click Refresh or restart Streamlit |

## Testing Commands

```bash
# Full system test
cd /home/jovyan/Inflation-Busting_Recipe_Generator && python << 'EOF'
from chatbot import get_all_ingredients, generate_recommendations_with_reasons
ingredients = get_all_ingredients()
print(f"✓ {len(ingredients)} ingredients loaded")
recs = generate_recommendations_with_reasons("Veg", ingredients, count=6)
print(f"✓ {len(recs)} recipes generated")
for i, rec in enumerate(recs, 1):
    print(f"  {i}. {rec.get('name', 'Unknown')}")
