from typing import Dict, Any, AsyncIterable, Optional, Union
from dataclasses import dataclass, fields, asdict, replace
import time
from multi_agent_orchestrator.utils.logger import Logger
from multi_agent_orchestrator.types import ConversationMessage, ParticipantRole, OrchestratorConfig
from multi_agent_orchestrator.classifiers import (Classifier,
                             ClassifierResult,
                             BedrockClassifier,
                             BedrockClassifierOptions)
from multi_agent_orchestrator.agents import (Agent,
                        AgentResponse,
                        AgentProcessingResult,
                        BedrockLLMAgent,
                        BedrockLLMAgentOptions)
from multi_agent_orchestrator.storage import ChatStorage, InMemoryChatStorage

DEFAULT_CONFIG=OrchestratorConfig()

@dataclass
class MultiAgentOrchestrator:
    def __init__(self,
                 options: OrchestratorConfig = DEFAULT_CONFIG,
                 storage: ChatStorage = InMemoryChatStorage(),
                 classifer: Classifier = BedrockClassifier(options=BedrockClassifierOptions()),
                 logger: Logger = None):

        if options is None:
            options = {}
        if isinstance(options, dict):
            # Filter out keys that are not part of OrchestratorConfig fields
            valid_keys = {f.name for f in fields(OrchestratorConfig)}
            options = {k: v for k, v in options.items() if k in valid_keys}
            options = OrchestratorConfig(**options)
        elif not isinstance(options, OrchestratorConfig):
            raise ValueError("options must be a dictionary or an OrchestratorConfig instance")


        self.config = replace(DEFAULT_CONFIG, **asdict(options))
        self.storage = storage


        self.logger = Logger(self.config, logger)
        self.agents: Dict[str, Agent] = {}
        self.classifier: Classifier = classifer
        self.execution_times: Dict[str, float] = {}
        self.default_agent: Agent = BedrockLLMAgent(
            options=BedrockLLMAgentOptions(
                name="DEFAULT",
                streaming=True,
                description="A knowledgeable generalist capable of addressing a wide range of topics.",
            ))


    def add_agent(self, agent: Agent):
        if agent.id in self.agents:
            raise ValueError(f"An agent with ID '{agent.id}' already exists.")
        self.agents[agent.id] = agent
        self.classifier.set_agents(self.agents)

    def get_default_agent(self) -> Agent:
        return self.default_agent

    def set_default_agent(self, agent: Agent):
        self.default_agent = agent

    def set_classifier(self, intent_classifier: Classifier):
        self.classifier = intent_classifier

    def get_all_agents(self) -> Dict[str, Dict[str, str]]:
        return {key: {
            "name": agent.name,
            "description": agent.description
        } for key, agent in self.agents.items()}

    async def dispatch_to_agent(self,
                                params: Dict[str, Any]) -> Union[
                                    ConversationMessage, AsyncIterable[Any]
                                ]:
        user_input = params['user_input']
        user_id = params['user_id']
        session_id = params['session_id']
        classifier_result:ClassifierResult = params['classifier_result']
        additional_params = params.get('additional_params', {})

        if not classifier_result.selected_agent:
            return "I'm sorry, but I need more information to understand your request. \
                Could you please be more specific?"

        selected_agent = classifier_result.selected_agent
        agent_chat_history = await self.storage.fetch_chat(user_id, session_id, selected_agent.id)

        self.logger.print_chat_history(agent_chat_history, selected_agent.id)
        self.logger.info(f"Routing intent '{user_input}' to {selected_agent.id} ...")

        response = await self.measure_execution_time(
            f"Agent {selected_agent.name} | Processing request",
            lambda: selected_agent.process_request(user_input,
                                                   user_id,
                                                   session_id,
                                                   agent_chat_history,
                                                   additional_params)
        )

        return response

    async def route_request(self,
                            user_input: str,
                            user_id: str,
                            session_id: str,
                            additional_params: Dict[str, str] = {}) -> AgentResponse:
        self.execution_times.clear()
        chat_history = await self.storage.fetch_all_chats(user_id, session_id) or []

        try:
            classifier_result:ClassifierResult = await self.measure_execution_time(
                "Classifying user intent",
                lambda: self.classifier.classify(user_input, chat_history)
            )

            if self.config.LOG_CLASSIFIER_OUTPUT:
                self.print_intent(user_input, classifier_result)

        except Exception as error:
            self.logger.error("Error during intent classification:", error)
            return AgentResponse(
                metadata=self.create_metadata(None,
                                              user_input,
                                              user_id,
                                              session_id,
                                              additional_params),
                output=self.config.CLASSIFICATION_ERROR_MESSAGE,
                streaming=False
            )

        if not classifier_result.selected_agent:
            if self.config.USE_DEFAULT_AGENT_IF_NONE_IDENTIFIED:
                classifier_result = self.get_fallback_result()
                self.logger.info("Using default agent as no agent was selected")
            else:
                return AgentResponse(
                    metadata= self.create_metadata(classifier_result,
                                                   user_input,
                                                   user_id,
                                                   session_id,
                                                   additional_params),
                    output= self.config.NO_SELECTED_AGENT_MESSAGE,
                    streaming=False
                )

        try:
            agent_response = await self.dispatch_to_agent({
                "user_input": user_input,
                "user_id": user_id,
                "session_id": session_id,
                "classifier_result": classifier_result,
                "additional_params": additional_params
            })

            metadata = self.create_metadata(classifier_result,
                                            user_input,
                                            user_id,
                                            session_id,
                                            additional_params)

            # save question
            await self.save_message(
                ConversationMessage(
                    role=ParticipantRole.USER.value,
                    content=[{'text':user_input}]
                ),
                user_id,
                session_id,
                classifier_result.selected_agent
            )

            if isinstance(agent_response, ConversationMessage):
                # save the response
                await self.save_message(agent_response,
                                        user_id,
                                        session_id,
                                        classifier_result.selected_agent)


            return AgentResponse(
                    metadata=metadata,
                    output=agent_response,
                    streaming=False
                )

        except Exception as error:
            self.logger.error("Error during agent dispatch or processing:", error)
            return AgentResponse(
                    metadata= self.create_metadata(classifier_result,
                                                   user_input,
                                                   user_id,
                                                   session_id,
                                                   additional_params),
                    output= self.config.GENERAL_ROUTING_ERROR_MSG_MESSAGE,
                    streaming=False
                )

        finally:
            self.logger.print_execution_times(self.execution_times)


    def print_intent(self, user_input: str, intent_classifier_result: ClassifierResult) -> None:
        """Print the classified intent."""
        Logger.log_header('Classified Intent')
        Logger.logger.info(f"> Text: {user_input}")
        Logger.logger.info(f"> Selected Agent: {intent_classifier_result.selected_agent.name \
                                                if intent_classifier_result.selected_agent \
                                                    else 'No agent selected'}")
        Logger.logger.info(f"> Confidence: {intent_classifier_result.confidence:.2f}")
        Logger.logger.info('')

    async def measure_execution_time(self, timer_name: str, fn):
        if not self.config.LOG_EXECUTION_TIMES:
            return await fn()

        start_time = time.time()
        self.execution_times[timer_name] = start_time

        try:
            result = await fn()
            end_time = time.time()
            duration = end_time - start_time
            self.execution_times[timer_name] = duration
            return result
        except Exception as error:
            end_time = time.time()
            duration = end_time - start_time
            self.execution_times[timer_name] = duration
            raise error

    def create_metadata(self,
                        intent_classifier_result: Optional[ClassifierResult],
                        user_input: str,
                        user_id: str,
                        session_id: str,
                        additional_params: Dict[str, str]) -> AgentProcessingResult:
        base_metadata = AgentProcessingResult(
            user_input=user_input,
            agent_id="no_agent_selected",
            agent_name="No Agent",
            user_id=user_id,
            session_id=session_id,
            additional_params=additional_params
        )

        if not intent_classifier_result or not intent_classifier_result.selected_agent:
            base_metadata.additional_params['error_type'] = 'classification_failed'
        else:
            base_metadata.agent_id = intent_classifier_result.selected_agent.id
            base_metadata.agent_name = intent_classifier_result.selected_agent.name

        return base_metadata

    def get_fallback_result(self) -> ClassifierResult:
        return ClassifierResult(selected_agent=self.get_default_agent(), confidence=0)

    async def save_message(self,
                           message: ConversationMessage,
                           user_id: str, session_id: str,
                           agent: Agent):
        if agent and agent.save_chat:
            return await self.storage.save_chat_message(user_id,
                                                        session_id,
                                                        agent.id,
                                                        message,
                                                        self.config.MAX_MESSAGE_PAIRS_PER_AGENT)