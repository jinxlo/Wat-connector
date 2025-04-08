# -*- coding: utf-8 -*-
import logging
import json
import time

_logger = logging.getLogger(__name__)

try:
    import openai
except ImportError:
    # Set openai to None or a placeholder object if you want to handle errors gracefully later
    openai = None
    _logger.warning("OpenAI Python library not found. Please install it (`pip install openai`) for GPT enrichment.")


# Modified parameter name for clarity to match the calling function
def call_openai_enrichment(product_name, available_category_names, api_key, model='gpt-3.5-turbo'):
    """
    Enriches product data using OpenAI's GPT model. Instructs GPT to choose category only from provided list.

    Args:
        product_name (str): The title/name of the product.
        available_category_names (list): A list of category names fetched live from WooCommerce.
                                         GPT will be instructed to choose only from this list.
        api_key (str): The OpenAI API key.
        model (str): The OpenAI model name to use (e.g., 'gpt-3.5-turbo', 'gpt-4').

    Returns:
        dict: A dictionary containing 'description', 'brand', and 'category',
              or None if enrichment fails. The category should be one of the names
              from the provided 'available_category_names' list or null/empty string.
              Brand can also be null/empty string.
    """
    if not openai:
        _logger.warning("OpenAI SDK not installed or failed to import. GPT enrichment skipped for '%s'.", product_name)
        return None

    if not api_key:
        _logger.warning("OpenAI API key is missing. GPT enrichment skipped for '%s'.", product_name)
        return None

    # Check if the provided category list is empty, which might affect GPT's ability to choose
    if not available_category_names:
        _logger.warning("The list of available WooCommerce categories provided to GPT is empty for product '%s'. Category selection may fail or return null.", product_name)
        # We still proceed as description/brand might be generated.

    start_time = time.time()
    try:
        # --- OpenAI Client Initialization (Handles SDK v1.x+) ---
        # Check if the modern client class exists
        if hasattr(openai, 'OpenAI'):
            # Use slightly longer timeout from the enhanced version
            client = openai.OpenAI(api_key=api_key, timeout=35.0)
        # Fallback for older versions (though v1+ is recommended)
        elif hasattr(openai, 'api_key'):
            openai.api_key = api_key
            # Note: Older versions might not support all new features like response_format
            client = openai # Use the module itself as the client
            _logger.warning("Using legacy OpenAI SDK structure. Consider upgrading (`pip install --upgrade openai`).")
        else:
            _logger.error("Unsupported OpenAI SDK structure found.")
            return None
        # --- End Client Initialization ---


        # --- Prompt Construction (Using the enhanced version) ---
        # Ensure the list is properly formatted for the prompt
        if available_category_names:
             # Escape potential quotes within category names if necessary, though unlikely
             formatted_categories = [f'"{name}"' for name in available_category_names]
             category_str = ', '.join(formatted_categories)
        else:
             category_str = 'ninguna disponible' # From enhanced prompt

        # <<< ENHANCED PROMPT from the first code block >>>
        prompt = f"""
Eres un experto catalogador de productos para una tienda online de electrodomésticos y artículos del hogar (WooCommerce). Tu tarea es generar metadatos precisos en formato JSON.

**Contexto Importante:** La tienda vende una variedad de productos, desde grandes electrodomésticos (Neveras, Cocinas) hasta pequeños artículos de cocina, cuidado personal y accesorios diversos.

**Instrucciones Estrictas:**
1.  **Analiza el título del producto:** "{product_name}"
2.  **Identifica la Marca:** Si la marca está claramente en el título (ej: Westinghouse, Oster, Samsung), extráela. Si no hay marca evidente, usa `null` para el valor de "brand".
3.  **Selecciona la Categoría MÁS ADECUADA:** Elige **UNA ÚNICA** categoría **EXCLUSIVAMENTE** de la siguiente lista oficial de categorías disponibles: [{category_str}]. **NO INVENTES** categorías. **Prioriza la función principal del producto**, no solo la marca. Por ejemplo, un abrelatas manual, aunque sea de marca 'Westinghouse', pertenece a 'Articulos del Hogar', no a 'Cocina electricas'. Si ninguna categoría de la lista encaja bien, usa `null` para el valor de "category".
4.  **Genera Descripción:** Crea una descripción corta (máximo 60 palabras), atractiva y útil en formato HTML (párrafo `<p>`), en español. Destaca beneficios o características clave.
5.  **Formato de Salida:** Responde **ÚNICAMENTE** con un objeto JSON válido con las claves exactas "description", "brand", y "category". Sin texto adicional antes o después del JSON.

**Ejemplos de Categorización Correcta:**
- Título: "Licuadora Oster Xpert Series Roja", Marca: "Oster", Categoría Correcta: "Licuadoras"
- Título: "Set De Clips Oreo Magneticos 2 Pzas Azul", Marca: "Oreo", Categoría Correcta: "Articulos del Hogar" (o "Accesorios" si es más adecuado y está en la lista)
- Título: "Abrelatas Westinghouse Mango Ergonomico Negro", Marca: "Westinghouse", Categoría Correcta: "Articulos del Hogar"
- Título: "Smart TV Samsung 55 pulgadas Crystal UHD", Marca: "Samsung", Categoría Correcta: "Televisores"

**JSON para el producto "{product_name}":**
"""
        # Use enhanced logging including truncated category list
        _logger.debug("Sending prompt to OpenAI for product '%s'. Model: %s. Category List: [%s]", product_name, model, category_str[:200] + "...")
        # --- End Prompt Construction ---


        # --- OpenAI API Call (Using ChatCompletion) ---
        # Prefer the modern SDK structure if available
        if hasattr(client, 'chat') and hasattr(client.chat, 'completions'):
            response = client.chat.completions.create(
                model=model,
                messages=[
                    # System message from enhanced version
                    {"role": "system", "content": "Generas metadatos de producto en formato JSON siguiendo instrucciones estrictas."},
                    {"role": "user", "content": prompt}
                ],
                # Use lower temperature from enhanced version
                temperature=0.2,
                max_tokens=350, # Kept slightly higher max_tokens
                # Request JSON output format (supported by newer models like gpt-3.5-turbo-1106+)
                response_format={"type": "json_object"}
            )
            content = response.choices[0].message.content
            # Optional: Log token usage if available
            if response.usage:
                _logger.debug("OpenAI API usage for '%s': %s", product_name, response.usage)

        # Fallback for legacy client (might not support response_format)
        elif hasattr(client, 'ChatCompletion'):
            # Note: Legacy might not respect response_format, increasing risk of non-JSON output
            response = client.ChatCompletion.create(
                model=model,
                 messages=[
                    {"role": "system", "content": "Generas metadatos de producto en formato JSON siguiendo instrucciones estrictas."}, # Added system prompt here too
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2, # Use lower temperature
                max_tokens=350
            )
            content = response.choices[0].message['content']
            # Token usage might be structured differently in older responses
            # _logger.debug("OpenAI API usage (legacy): %s", response.get('usage'))
        else:
             _logger.error("Failed to find a compatible OpenAI API call method.")
             return None
        # --- End API Call ---

        # --- Response Processing ---
        if not content:
            _logger.error("OpenAI returned empty content for '%s'.", product_name)
            return None

        # Clean potential markdown code fences or extra whitespace
        cleaned_content = content.strip().strip("```json").strip("```").strip()

        # <<< ADDED LOGGING: See the actual JSON response (from enhanced) >>>
        _logger.debug("Raw JSON response received from OpenAI for '%s':\n%s", product_name, cleaned_content)
        # <<< END LOGGING >>>

        # Attempt to parse the JSON
        try:
            result_json = json.loads(cleaned_content)
        except json.JSONDecodeError as json_err:
            _logger.error("Failed to decode JSON response from OpenAI for '%s'. Error: %s\nRaw Content:\n%s",
                          product_name, json_err, cleaned_content)
            return None

        # Validate the structure (check if it's a dict and has the expected keys)
        if not isinstance(result_json, dict) or not all(key in result_json for key in ["description", "brand", "category"]):
             _logger.warning("OpenAI response JSON for '%s' is missing expected keys or is not a dictionary. Received: %s", product_name, result_json)
             # Decide how to handle - return partial data or None? Returning None is safer.
             return None

        # <<< ADDED LOGGING: See the parsed dictionary (from enhanced) >>>
        _logger.debug("Parsed JSON data for '%s': %s", product_name, result_json)
        # <<< END LOGGING >>>

        end_time = time.time()
        _logger.info("Successfully received and parsed GPT enrichment for '%s' in %.2f seconds.", product_name, end_time - start_time)
        return result_json
        # --- End Response Processing ---

    # Catch specific OpenAI errors including product name in log (from enhanced version)
    except openai.RateLimitError as e:
        _logger.error("OpenAI Rate Limit Exceeded during enrichment for '%s': %s", product_name, e)
        return None
    except openai.AuthenticationError as e:
        _logger.error("OpenAI Authentication Error (check API key) for '%s': %s", product_name, e)
        return None
    except Exception as e:
        _logger.error("General OpenAI API call failed for '%s': %s", product_name, e, exc_info=True)
        return None