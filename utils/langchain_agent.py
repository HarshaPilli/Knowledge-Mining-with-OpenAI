import os
import pickle
import numpy as np
import tiktoken
import json
import logging
import re
import uuid
import urllib

from langchain.llms.openai import AzureOpenAI
from langchain.agents import initialize_agent, Tool, load_tools, AgentExecutor
from langchain.llms import OpenAI
from langchain.prompts.prompt import PromptTemplate
from langchain import LLMMathChain
from langchain.prompts import PromptTemplate, BasePromptTemplate
from langchain.agents.mrkl.base import ZeroShotAgent
from typing import Any, Callable, List, NamedTuple, Optional, Sequence, Tuple
from langchain.tools.base import BaseTool
from langchain.schema import AgentAction, AgentFinish
from langchain.memory import ConversationBufferMemory

from utils.langchain_helpers.oldschoolsearch import OldSchoolSearch
from utils.langchain_helpers.mod_agent import GPT35TurboAzureOpenAI, ZSReAct, ReAct, ModBingSearchAPIWrapper
import utils.langchain_helpers.mod_react_prompt

from utils import openai_helpers
from utils.language import extract_entities
from utils import redis_helpers
from utils import storage

from utils.helpers import redis_search, redis_lookup
from utils.cogsearch_helpers import cog_search, cog_lookup
from multiprocessing.dummy import Pool as ThreadPool


AZURE_OPENAI_SERVICE = os.environ.get("OPENAI_RESOURCE_ENDPOINT") 
OPENAI_API_KEY= os.environ.get("OPENAI_API_KEY")
DAVINCI_003_COMPLETIONS_MODEL = os.environ.get("DAVINCI_003_COMPLETIONS_MODEL")
CHOSEN_COMP_MODEL = os.environ.get("CHOSEN_COMP_MODEL")
COG_SEARCH_ENDPOINT= os.environ.get("COG_SEARCH_ENDPOINT")
COG_SEARCH_ADMIN_KEY= os.environ.get("COG_SEARCH_ADMIN_KEY")
KB_SEM_INDEX_NAME = os.environ.get("KB_SEM_INDEX_NAME")
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS"))
CHOSEN_EMB_MODEL = os.environ.get("CHOSEN_EMB_MODEL")
MAX_QUERY_TOKENS = int(os.environ.get("MAX_QUERY_TOKENS"))
MAX_HISTORY_TOKENS = int(os.environ.get("MAX_HISTORY_TOKENS"))
USE_BING = os.environ.get("USE_BING")
CONVERSATION_TTL_SECS = int(os.environ.get("CONVERSATION_TTL_SECS"))

import openai

openai.api_type = "azure"
openai.api_key = os.environ["OPENAI_API_KEY"]
openai.api_base = os.environ["OPENAI_RESOURCE_ENDPOINT"]
openai.api_version = "2022-12-01"

DEFAULT_RESPONSE = "Sorry, the question was not clear, or the information is not in the knowledge base. Please rephrase your question."


pool = ThreadPool(6)




class KMOAI_Agent():

    def __init__(self, enable_unified_search = True, enable_redis_search=True, enable_cognitive_search=True, evaluate_step = True, 
                       agent_name = "zs", check_adequacy=True, verbose=True):

        self.enable_unified_search = enable_unified_search
        self.redis_filter_param = '*'
        self.cogsearch_filter_param = None
        self.evaluate_step = evaluate_step
        self.agent_name = agent_name
        self.check_adequacy = check_adequacy

        self.turbo_llm = GPT35TurboAzureOpenAI(deployment_name=CHOSEN_COMP_MODEL, temperature=0, openai_api_key=openai.api_key, max_retries=5, request_timeout=30, stop=['<|im_end|>'], max_tokens=MAX_OUTPUT_TOKENS)

        zs_tools = []

        if enable_unified_search: 
            zs_tools += [
                Tool(name="Unified Search", func=self.unified_search, description="useful for when you need to start a search to answer questions from the knowledge base")
            ]
        
        if enable_redis_search: 
            zs_tools += [
                Tool(name="Redis Search", func=self.agent_redis_search, description="useful for when you need to answer questions from the Redis system"),
            ]

        if enable_cognitive_search: 
            zs_tools += [
                Tool(name="Cognitive Search", func=self.agent_cog_search, description="useful for when you need to answer questions from the Cognitive system"),
                Tool(name="Cognitive Lookup", func=self.agent_cog_lookup, description="useful for when you need to lookup terms from the the Cognitive system"),            
            ]


        if USE_BING == 'yes':
            self.bing_search = ModBingSearchAPIWrapper(k=10)
            zs_tools.append(Tool(name="Online Search", func=self.agent_bing_search, description='useful for when you need to answer questions about current events from the internet'),)
        else:
            self.bing_search = None

        ds_tools = [
            Tool(name="Search", func=self.unified_search, description="useful for when you need to answer questions"),
            Tool(name="Lookup", func=self.agent_cog_lookup, description="useful for when you need to lookup terms")
        ]


        self.memory = ConversationBufferMemory(memory_key="history")

        self.zs_agent = ZSReAct.from_llm_and_tools(self.turbo_llm, zs_tools)
        self.zs_chain = AgentExecutor.from_agent_and_tools(self.zs_agent, zs_tools, verbose=verbose, return_intermediate_steps = verbose, 
                                                                                                max_iterations = 8, early_stopping_method="generate" )

        self.ds_agent = ReAct.from_llm_and_tools(self.turbo_llm, ds_tools)
        self.ds_chain = AgentExecutor.from_agent_and_tools(self.ds_agent, ds_tools, verbose=verbose, return_intermediate_steps = verbose, 
                                                                                                max_iterations = 8, early_stopping_method="generate" )

        completion_enc = openai_helpers.get_encoder(CHOSEN_COMP_MODEL)

        zs_pr = self.zs_agent.create_prompt([]).format(history='', input='', agent_scratchpad='', pre_context='')
        ds_pr = self.ds_agent.create_prompt([]).format(history='', input='', agent_scratchpad='', pre_context='')

        self.zs_empty_prompt_length = len(completion_enc.encode(zs_pr))
        self.ds_empty_prompt_length = len(completion_enc.encode(ds_pr))


    def agent_redis_search(self, query):
        response = redis_helpers.redis_get(self.redis_conn, query, 'redis_search_response')

        if response is None:
            response = '\n\n'.join(redis_search(query, self.redis_filter_param))
            response = self.evaluate(query, response)
            redis_helpers.redis_set(self.redis_conn, query, 'redis_search_response', response, CONVERSATION_TTL_SECS)
        else:
            response = response.decode('UTF-8')

        return response


    def agent_redis_lookup(self, query):
        response = redis_helpers.redis_get(self.redis_conn, query, 'redis_lookup_response')

        if response is None:
            response = '\n\n'.join(redis_lookup(query, self.redis_filter_param))
            response = self.evaluate(query, response)
            redis_helpers.redis_set(self.redis_conn, query, 'redis_lookup_response', response, CONVERSATION_TTL_SECS)
        else:
            response = response.decode('UTF-8')

        return response


    def agent_cog_search(self, query):
        response = redis_helpers.redis_get(self.redis_conn, query, 'cog_search_response')

        if response is None:
            response = '\n\n'.join(cog_search(query, self.cogsearch_filter_param))
            response = self.evaluate(query, response)
            redis_helpers.redis_set(self.redis_conn, query, 'cog_search_response', response, CONVERSATION_TTL_SECS)
        else:
            response = response.decode('UTF-8')

        return response



    def agent_cog_lookup(self, query):
        response = redis_helpers.redis_get(self.redis_conn, query, 'cog_lookup_response')

        if response is None:
            response = '\n\n'.join(cog_lookup(query, self.cogsearch_filter_param))
            response = self.evaluate(query, response)
            redis_helpers.redis_set(self.redis_conn, query, 'cog_lookup_response', response, CONVERSATION_TTL_SECS)
        else:
            response = response.decode('UTF-8')

        return response


    def agent_bing_search(self, query):
        if USE_BING == 'yes':
            response = redis_helpers.redis_get(self.redis_conn, query, 'bing_search_response')

            if response is None:
                response = '\n\n'.join(self.bing_search.run(query))
                response = self.evaluate(query, response)
                redis_helpers.redis_set(self.redis_conn, query, 'bing_search_response', response, CONVERSATION_TTL_SECS)
            else:
                response = response.decode('UTF-8')

            return response
        else:
            return ''


    def evaluate(self, query, context):
        if self.evaluate_step:
            completion_enc = openai_helpers.get_encoder(CHOSEN_COMP_MODEL)
            max_comp_model_tokens = openai_helpers.get_model_max_tokens(CHOSEN_COMP_MODEL)

            query_len = len(completion_enc.encode(query))
            empty_prompt = len(completion_enc.encode(utils.langchain_helpers.mod_react_prompt.mod_evaluate_instructions.format(context = "", question = "")))
            allowance = max_comp_model_tokens - empty_prompt - MAX_OUTPUT_TOKENS - query_len
            
            context = completion_enc.decode(completion_enc.encode(context)[:allowance]) 
            prompt = utils.langchain_helpers.mod_react_prompt.mod_evaluate_instructions.format(context = context, question = query)
            response = openai_helpers.contact_openai(prompt, CHOSEN_COMP_MODEL, MAX_OUTPUT_TOKENS)
        else:
            response = context

        return response 

    def qc(self, query, answer):
        prompt = utils.langchain_helpers.mod_react_prompt.mod_qc_instructions.format(answer = answer, question = query)
        response = openai_helpers.contact_openai(prompt, CHOSEN_COMP_MODEL, MAX_OUTPUT_TOKENS)
        response = response.strip().replace(',', '').replace('.', '').lower().replace("<|im_end|>", '')
        print(f"Is the answer adequate: {response}")
        if response == "no": print(answer)
        return response 


    def chichat(self, query):
        prompt = utils.langchain_helpers.mod_react_prompt.mod_chit_chat_instructions.format(question = query)
        response = openai_helpers.contact_openai(prompt, CHOSEN_COMP_MODEL, MAX_OUTPUT_TOKENS)
        response = response.strip().replace(',', '').replace('.', '').lower().replace("<|im_end|>", '')
        return response 


    def unified_search(self, query):

        response = redis_helpers.redis_get(self.redis_conn, query, 'response')

        if response is None:
            list_f = ['redis_search', 'cog_lookup', 'cog_search']
            list_q = [query for f in list_f]

            if USE_BING == 'yes':
                list_f += ['bing_lookup']
                list_q += [query]
            
            # print(list_f, list_q)

            results = pool.starmap(self.specific_search,  zip(list_q, list_f))

            max_items = max([len(r) for r in results])

            final_context = []
            context_dict = {}

            for i in range(max_items):
                for j in range(len(results)):
                    if i < len(results[j]): 
                        if results[j][i] not in context_dict:
                            context_dict[results[j][i]] = 1
                            final_context.append(results[j][i])

            response = '\n\n'.join(final_context)   
            response = self.evaluate(query, response)

            redis_helpers.redis_set(self.redis_conn, query, 'response', response, CONVERSATION_TTL_SECS)
        else:
            response = response.decode('UTF-8')
 
        return response



    def specific_search(self, q, func_name):
        if func_name == "redis_search": return redis_search(q, self.redis_filter_param)
        if func_name == "cog_lookup": return cog_lookup(q, self.cogsearch_filter_param)
        if func_name == "cog_search": return cog_search(q, self.cogsearch_filter_param)

        if USE_BING == 'yes':
            if func_name == "bing_lookup": return self.bing_search.run(q)


    def replace_occurrences(self, answer, occ):
        matches = re.findall(occ, answer, re.DOTALL)            
        for m in matches:
            try:
                if isinstance(m, tuple): m = ' '.join(m).rstrip()
                answer = answer.replace(m, '')        
            except Exception as e:
                print(m, occ, e)
        return answer

    def process_final_response(self, query, response):
        if isinstance(response, str):
            answer = response
        else:    
            answer = response.get('output')

        occurences = [
            "Action:[\n\r\s]+(.*?)[\n]*[\n\r\s](.*)"
            "Action Input:[\s\r\n]+",
            "Action:[\s\r\n]+None needed?.",
            "Action:[\s\r\n]+None?.",
            "Action:[\s\r\n]+",
            "Action [\d]+:",
            "Action Input:",
            "Online Search:",
            "Thought [0-9]+:",
            "Observation [0-9]+:",
            "Final Answer:",
            "Final Answer",
            "Finish\[",
            "Human:",
            "AI:",
            "--",
            "###"
        ]

        for occ in occurences:
            answer = self.replace_occurrences(answer, occ)
            
        answer = answer.replace('<|im_end|>', '')

        tools_occurences = [
            'Redis Search',
            'Cognitive Search'
            'Online Search'
        ]

        for occ in tools_occurences:
            answer = answer.replace(occ, 'the knowledge base')

        sources = []

        source_matches = re.findall(r'\((.*?)\)', answer)  
        source_matches += re.findall(r'\[(.*?)\]', answer)
        
        for s in source_matches:
            answer = answer.replace('('+s+')', '')
            answer = answer.replace('['+s+']', '')
            try:
                arr = s.split('/')
                sas_link = storage.create_sas_from_container_and_blob(arr[0], arr[1])
                sources.append(sas_link)
            except:
                if s.startswith("https://"): sources.append(s)

          
        # for s in source_matches:
        #     answer = answer.replace('['+s+']', '')
        #     try:
        #         arr = s.split('/')
        #         sas_link = storage.create_sas_from_container_and_blob(arr[0], arr[1])
        #         sources.append(sas_link)
        #     except:
        #         if s.startswith("https://"): sources.append(s)

        answer = answer.replace('[', '').replace(']','').rstrip()

        if answer == '':
            answer = DEFAULT_RESPONSE

        self.memory.save_context({"input": query}, {"output": answer})

        return answer, sources



    def get_history(self, prompt_id):

        if (prompt_id is None) or (prompt_id == ''):
            hist = ''
            prompt_id = str(uuid.uuid4())
            # prompt_id = "prompt_id"
            # print("PROMPT ID", prompt_id)
        else:
            rhist = redis_helpers.redis_get(self.redis_conn, prompt_id, 'history')
            if rhist is None:
                hist = ''
            else:
                hist = rhist.decode('utf-8')

        return hist, prompt_id
        

    def manage_history(self, hist, prompt_id):

        new_hist = self.memory.load_memory_variables({})['history']
        hist = hist + '\n' + new_hist
        #hist = hist.replace("Human:", "user:").replace("AI:", "assistant:")
        
        completion_enc = openai_helpers.get_encoder(CHOSEN_COMP_MODEL)
        hist_enc = completion_enc.encode(hist)
        hist_enc_len = len(hist_enc)

        if hist_enc_len > MAX_HISTORY_TOKENS * 0.85:
            # print("SUMMARIZING")
            hist = openai_helpers.openai_summarize(hist, CHOSEN_COMP_MODEL).replace('<|im_end|>', '')

        if hist_enc_len > MAX_HISTORY_TOKENS:
            hist = completion_enc.decode(hist_enc[hist_enc_len - MAX_HISTORY_TOKENS :])

        redis_helpers.redis_set(self.redis_conn, prompt_id, 'history', hist, CONVERSATION_TTL_SECS)



    def inform_agent_input_lengths(self, agent, query, history, pre_context):
        completion_enc = openai_helpers.get_encoder(CHOSEN_COMP_MODEL)
        agent.query_length        = len(completion_enc.encode(query))
        agent.history_length      = len(completion_enc.encode(history))
        agent.pre_context_length  = len(completion_enc.encode(pre_context))




    def assign_filter_param(self, filter_param):

        if filter_param is None:
            self.redis_filter_param = '*'
            self.cogsearch_filter_param = None
        else:
            self.redis_filter_param = filter_param
            self.cogsearch_filter_param = filter_param


    def process_request(self, query, hist, pre_context):
        
        try:
            if self.agent_name == 'zs':
                response = self.zs_chain({'input':query, 'history':hist, 'pre_context':pre_context}) 
            elif self.agent_name == 'ds':                
                response = self.ds_chain({'input':query, 'history':hist, 'pre_context':pre_context})
            elif self.agent_name == 'os':   
                response = OldSchoolSearch().search(query, hist, pre_context, filter_param=self.redis_filter_param, enable_unified_search=self.enable_unified_search, unified_search_owner=self)             
            else:
                response = self.zs_chain({'input':query, 'history':hist, 'pre_context':pre_context}) 


        except Exception as e:
            e_str = str(e)
            print("Exception 1st chain", e_str)

            if e_str.startswith("Could not parse LLM output:"):
                response = e_str.replace("Action: None", "").replace("Action:", "").replace('<|im_end|>', '')
            else:
                print("Exception 2nd chain", e)
                try:
                    response = self.zs_chain({'input':query, 'history':hist, 'pre_context':pre_context}) 
                except Exception as e:
                    try:
                        oss = OldSchoolSearch()
                        response = oss.search(query, hist, pre_context, filter_param=self.redis_filter_param, enable_unified_search=self.enable_unified_search, unified_search_owner=self)
                        
                    except Exception as e:
                        print("Exception 3rd chain", e)
                        try:
                            response = self.ds_chain({'input':query, 'history':hist, 'pre_context':pre_context})     
                        except Exception as e:
                            print("Exception 4th chain", e)
                            response = DEFAULT_RESPONSE  

        answer, sources = self.process_final_response(query, response)

        return answer, sources


    def get_pre_context(self, intent):
        
        if (intent is None) or (intent == ''):
            return ""
        else:
            pre_context = redis_helpers.redis_get(self.redis_conn, intent, 'answer')
            sources = redis_helpers.redis_get(self.redis_conn, intent, 'sources')

            if pre_context is None:
                return ""
            else:
                pre_context = pre_context.decode('utf-8')
                sources = sources.decode('utf-8')

        return f"[{sources}] {pre_context}"


    def get_intent(self, query):
        prompt = utils.langchain_helpers.mod_react_prompt.mod_extract_intent_instructions.format(question = query)
        response = openai_helpers.contact_openai(prompt, CHOSEN_COMP_MODEL, MAX_OUTPUT_TOKENS)

        output = response.strip().replace("<|im_end|>", '')

        intent_regex = "[iI]ntent:[\r\n\t\f\v ]+.*\n"
        output_regex = "[kK]eywords:[\r\n\t\f\v ]+.*"

        try:
            intent = re.search(intent_regex, output, re.DOTALL)
            keywords = re.search(output_regex, output, re.DOTALL)
            intent, keywords = intent.group(0).replace('\n', '').replace('Intent:', '').strip(), keywords.group(0).replace('\n', '').replace('Keywords:', '').strip()
            intent, keywords = intent.replace(',', '').replace('.', '').strip(), keywords.replace(',', '').replace('.', '').strip()

            print('\n', 'Intent:', intent.strip(), '\n', 'Response:', keywords)

            return intent, keywords
        except:
            return 'knowledge base', None


    def run(self, query, prompt_id, redis_conn, filter_param):

        self.redis_conn = redis_conn
        
        hist, prompt_id = self.get_history(prompt_id)
        intent, intent_output = self.get_intent(query)
        print("Intent:", intent, '-', intent_output)

        if intent == "chit chat":
            return self.chichat(query), "", prompt_id

        pre_context = self.get_pre_context(intent_output)
        print(f"Inserting history: {hist}")
        print(f"Inserting pre-context: {pre_context}")

        self.assign_filter_param(filter_param)
        self.inform_agent_input_lengths(self.zs_chain.agent, query, hist, pre_context)
        self.inform_agent_input_lengths(self.ds_chain.agent, query, hist, pre_context)

        answer, sources = self.process_request(query, hist, pre_context)

        if not self.check_adequacy:
            return answer, sources, prompt_id
        
        tries = 3
        adequate = "no"

        while tries > 0:
            adequate = self.qc(query, answer)

            if adequate == "no":
                answer, sources = self.process_request(query, hist, pre_context)
                tries -= 1
            else:
                self.manage_history(hist, prompt_id)
                redis_helpers.redis_set(self.redis_conn, intent_output, 'answer', answer, CONVERSATION_TTL_SECS)
                redis_helpers.redis_set(self.redis_conn, intent_output, 'sources', ','.join(sources), CONVERSATION_TTL_SECS)

                return answer, sources, prompt_id

        return DEFAULT_RESPONSE, [], prompt_id


        




        






