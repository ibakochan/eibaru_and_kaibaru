import React, { useState, useEffect, useReducer, useRef } from 'react';
import axios from 'axios';
import { Modal, Button } from 'react-bootstrap';
import { useCookies } from 'react-cookie';
import { Link } from 'react-router-dom';
import { FaPlay, FaArrowLeft } from 'react-icons/fa';

import { useUser } from "../context/UserContext";
import { useWebsocket } from "../context/WebsocketContext";
import { useTest } from "../context/TestContext";
import { gameReducer, initialGameState } from "../context/GameReducer";

import { formatTextAsHtml, wrapTextSafe, shuffleArray, getRandomizedValues, getRandomizedOptions, handlePlay, useAutoPlay } from "../utils/TestHelpers";
import { useScoreHelpers } from '../utils/TestRecordHelpers';
import { TestModal, ReturnConfirmModal, SignupPromptModal, InvitationModal } from '../utils/TestModalHelpers';
import { filterTests } from '../utils/filtering';


import { ws, connect, disconnect, sendData, setConnectedUsersCallback, setInvitationCallback, setInvitationResponseCallback, setUserNotFoundCallback, setCategoryToggleCallback, setTestToggleCallback, setQuestionsDataCallback, setAnswerSubmitCallback, setAnswerEchoSubmitCallback, setBattleTestsCallback } from '../websockets/websocketConnect';

import Practice from './TestChildren/Practice';
import TestRenderForm from './TestChildren/TestRenderForm';
import DisplayAllVocab from './TestChildren/DisplayAllVocab';
import Login from './TestChildren/Login';
import IbaruCharacters from './TestChildren/IbaruCharacters'
import { CategoryButtons, CategoryReturnButton } from "./TestChildren/CategoryTogglers";
import { IbaruCharactersButton, MusicVideoButton, FinalsButton, FinalsReturnButton, TestReturnButton, TestButtons } from './TestChildren/TestTogglers';


import { useToggleQuestionDetails, useToggleCategories } from '../hooks/testTogglers';
import useFetch from "../hooks/useFetch";
import { useHandleSubmit } from "../hooks/submitAnswer";
import { useMaxScores, useTestsByCategory, useQuestionsByTest, useTestQuestionsAndOptions } from '../hooks/useApiFetch';

const Test = () => {
  const [accessToken, setAccessToken] = useState(localStorage.getItem('access_token'));
  const { showInvitationModal, setShowInvitationModal, battleScore, setBattleScore, opponentScore, setOpponentScore, connected, connectedUsers, invitations, targetUsername, setTargetUsername, inviter, handleAcceptInvitation, handleDeclineInvitation, handleSendInvitation, opponentA, volume, setVolume, isPractice, setIsPractice, showModal, setShowModal, battleModalPicture, setBattleModalPicture, battleModalText, setBattleModalText, battleFinishMessage, setBattleFinishMessage } = useWebsocket();
  const { currentUser, setCurrentUser, activeTestId, setActiveTestId, lvl, setLvl, petLevel, setPetLevel, activeClassroomId, activeClassroomName, setActiveClassroomId, userClassrooms, setActiveClassroomName, activity, setActivity, isEnglish, setIsEnglish } = useUser();
  const { maxScores, setMaxScores, tests, setTests, testQuestions, setTestQuestions, totalQuestions, setTotalQuestions, questions, setQuestions, maximumScore, setMaximumScore, scoreMultiplier, setScoreMultiplier, activeTestMaxScore, setActiveTestMaxScore } = useTest();
  const { recordFinalsScores, recordScore, declareWinner } = useScoreHelpers();
  const [gameState, dispatchGame] = useReducer(gameReducer, initialGameState);
  const [inputValue, setInputValue] = useState('');
  const [selectedOption, setSelectedOption] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [activeMusicVideos, setActiveMusicVideos] = useState(false);
  const [activeIbaruCharacters, setActiveIbaruCharacters] = useState(false);
  const [activeFinals, setActiveFinals] = useState(false);
  const [activeTestName, setActiveTestName] = useState('');
  const [activeTestDescription, setActiveTestDescription] = useState('');
  const [activeTestDescriptionSound, setActiveTestDescriptionSound] = useState('');
  const [activeCategory, setActiveCategory] = useState(null);
  const [modalIsOpen, setModalIsOpen] = useState(false);
  const [signupModal, setSignupModal] = useState(false);
  const [isCorrect, setIsCorrect] = useState(null);
  const [currentCorrectAudioIndex, setCurrentCorrectAudioIndex] = useState(0);
  const [currentWrongAudioIndex, setCurrentWrongAudioIndex] = useState(0);
  const [recordMessage, setRecordMessage] = useState('');
  const [message, setMessage] = useState("");
  const [cookies, setCookie, removeCookie] = useCookies(['csrftoken']);
  const [correctAnswerKey, setCorrectAnswerKey] = useState('');
  const [correctWord, setCorrectWord] = useState('');
  const [correctSound, setCorrectSound] = useState('');
  const [correctPicture, setCorrectPicture] = useState('');
  const [correctLabel, setCorrectLabel] = useState('');
  const [correctEikenWord, setCorrectEikenWord] = useState('');
  const [randomizedValues, setRandomizedValues] = useState({});
  const [randomizedOptions, setRandomizedOptions] = useState({});
  const [correctOption, setCorrectOption] = useState(false);
  const [isPlayDisabled, setIsPlayDisabled] = useState(false);

  const [opponentAnsweredWrong, setOpponentAnsweredWrong] = useState(false);
  const [opponentAnsweredCorrect, setOpponentAnsweredCorrect] = useState(false);
  const [youAnsweredWrong, setYouAnsweredWrong] = useState(false);
  const [youAnsweredCorrect, setYouAnsweredCorrect] = useState(false);
  const [countdown, setCountdown] = useState(3);
  const [timestamp, setTimestamp] = useState(null);
  const [opponentATimestamp, setOpponentATimestamp] = useState(null);
  const [opponentATests, setOpponentATests] = useState([]);
  
  const [story, setStory] = useState('');
  const [storyPicture, setStoryPicture] = useState('');



  const currentQuestion = testQuestions.questions.filter(
    q =>
      (q.test === activeTestId && !q.category_on) ||
      (activeFinals && q.category_on === activeCategory)
  )[0];

  const currentMaxScore = maxScores.find(score => score.test === activeTestId);

  const scoreRatio =
    currentMaxScore && currentMaxScore.total_questions > 0
      ? currentMaxScore.score / currentMaxScore.total_questions
      : 0;


  const correctAudioUrls = window.correctAudioUrls;
  const wrongAudioUrls = window.wrongAudioUrls;
  const correctEnglishAudioUrls = window.correctEnglishAudioUrls;
  const wrongEnglishAudioUrls = window.wrongEnglishAudioUrls;
  const urlPath = window.urlPath

  let testType = "a";

  if (urlPath === "/ar/") {
    testType = "ar";
  }

  const audioRef = useRef(null);
  const testSectionRef = useRef(null);
  useEffect(() => {
    if (testSectionRef.current) {
      const element = testSectionRef.current;
      const rect = element.getBoundingClientRect();
      const scrollTop = window.pageYOffset || document.documentElement.scrollTop;
      const elementTop = rect.top + scrollTop;
      const offset = window.innerHeight / 2 - rect.height / 2;

      window.scrollTo({
        top: elementTop - offset,
        behavior: 'smooth'
      });
    }
  }, []);


  const { fetchData } = useFetch('', 'GET', null, setLoading, setError);

  const isEikenCategory = activeCategory === 'eiken' || activeCategory === 'eiken4';
  const isRealStoryTest = (activeTestName || '').includes('実話');
  const isGrammarOrLongDashTest = (activeTestName || '').includes('ー') || (activeTestName || '').includes('文法的語彙');
  const shouldShowEikenPracticeSection = isEikenCategory && isGrammarOrLongDashTest;
  const yourScore = gameState.scoreCounter * scoreMultiplier;
  const isEikenOrPhonics = activeCategory?.includes("eiken") || activeCategory?.includes("phonics");
  const isAboveMaxScore = yourScore > activeTestMaxScore;
  const isValidScore = yourScore >= 0.5 * maximumScore;
  const isBelowEikenThreshold = isEikenOrPhonics && yourScore < 0.7 * maximumScore;

  const activeQuestion = testQuestions.questions
  .filter(question => 
    (question.test === activeTestId && !question.category_on) || 
    (activeFinals && question.category_on === activeCategory)
  )
  .sort((a, b) => (b.sound3 ? 1 : 0) - (a.sound3 ? 1 : 0))
  [gameState.activeQuestionIndex];

  const questionData = activeQuestion ? randomizedValues[activeQuestion.duplicateId] || {} : {};

  const { randomAlphabetSliced, randomAlphabet, randomUrl, randomWrongOne, randomWrongTwo, randomWrongThree, randomFifth, randomCorrect, randomEikenUrl, randomTranslation, randomNumbers, randomWord, randomWord2, randomJapanese, randomPicture, randomSound, randomSound2, randomSound3, randomLabel } = questionData;

  const label = activeQuestion?.label;
  const sound2 = activeQuestion?.sound2;
  const sound3 = activeQuestion?.sound3;
  const word2 = activeQuestion?.word2;
  
  const isAudio = typeof randomUrl === 'string' && (randomUrl.includes('Record') || randomUrl.includes('mp3'));
  const isEikenAudio = typeof randomEikenUrl === 'string' && (randomEikenUrl.includes('Record') || randomEikenUrl.includes('mp3'));
  const isPicture = typeof randomUrl === 'string' && randomUrl.includes('image');

  const handleAutoPlay = useAutoPlay(isPlayDisabled, setIsPlayDisabled, testQuestions, activeTestId, activeFinals, 
    activeCategory, gameState, sound3, sound2, label, randomSound3, randomSound2, randomSound, randomUrl, audioRef);
  useEffect(() => {
    if (showModal && audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
    }
  }, [showModal]);
  
  const fetchMaxScores = useMaxScores(currentUser, setMaxScores, setLoading, setError);
  const fetchTestsByCategory = useTestsByCategory(setTests, setLoading, setError);
  const fetchQuestionsByTest = useQuestionsByTest(setQuestions, testType, setLoading, setError);
  const fetchTestQuestionsAndOptions = useTestQuestionsAndOptions(setQuestions, setTestQuestions, setTotalQuestions, setRandomizedOptions, setRandomizedValues, dispatchGame, setLoading, setError, activeCategory, testType);

  const filteredTests = filterTests(tests, maxScores, opponentA, opponentATests);
  
  const toggleQuestionDetails = useToggleQuestionDetails({
    testQuestions, setQuestions, setRandomizedValues, setRandomizedOptions,
    setActiveTestId, setActiveTestName, setActiveTestDescription, setActiveTestDescriptionSound,
    activeTestId, activeTestDescription, activeTestDescriptionSound, isPractice, activeFinals, activeCategory,
    fetchQuestionsByTest, setTotalQuestions, fetchTestQuestionsAndOptions, inviter, opponentA, setError, dispatchGame, setBattleScore, setOpponentScore, setMaximumScore, setScoreMultiplier, setActiveTestMaxScore,
  });

  const handleSubmit = useHandleSubmit({ws, sendData, timestamp, youAnsweredCorrect, setYouAnsweredCorrect, setCorrectWord, setCorrectSound, setCorrectPicture, setCorrectLabel, setCorrectEikenWord,
  setSelectedOption, setInputValue, setShowModal, setCorrectAnswerKey,
  setIsCorrect, setCurrentCorrectAudioIndex, currentCorrectAudioIndex, setCurrentWrongAudioIndex, currentWrongAudioIndex,
  setQuestions, activeFinals, isEnglish, volume,
  opponentA, correctOption, opponentAnsweredWrong,
  randomNumbers, randomWord, sound3, randomSound3, sound2, randomSound2, randomSound,
  randomEikenUrl, randomUrl, randomPicture, word2, randomWord2, randomLabel, randomCorrect,
  randomAlphabet, correctAudioUrls, correctEnglishAudioUrls,
  wrongAudioUrls, wrongEnglishAudioUrls, dispatchGame, gameState, setYouAnsweredWrong})
  
  const toggleCategories = useToggleCategories(inviter, ws, opponentA, activeCategory, setActiveCategory, setActivity, tests, fetchTestsByCategory, setError, maxScores, fetchMaxScores, sendData);

  const battleTests = Object.values(tests)
  .flat()
  .filter(test => {
    const maxScore = maxScores?.find(ms => ms.test === test.id);
    return maxScore && (maxScore.score / test.total_score) >= 0.7;
  });

  useEffect(() => {
    if (opponentA !== "" && ws && ws.readyState === WebSocket.OPEN) {
      sendData({
        type: "battle_tests",
        target_username: opponentA,
        battle_tests: inviter ? tests : battleTests,
      });
    }
  }, [currentUser, tests]);

  useEffect(() => {
    setBattleTestsCallback(data => {
      if (inviter) {
        setOpponentATests(data.battle_tests);
      } else if (opponentA !== "") {
        setTests(data.battle_tests);
      }
    });
  }, [currentUser, inviter, maxScores]);

  const openSignupModal = () => {
    setSignupModal(true);
  };

  const closeSignupModal = () => {
    setSignupModal(false);
  };
  

  const openModal = () => {
    if (isPractice) {
      setActiveTestDescription('')
      setActiveTestDescriptionSound('')
    }
    if (isPractice || gameState.activeQuestionIndex === 0 || opponentA !== "") {
      setActiveTestId(null);
      setActiveFinals(false);
    } else {
      setModalIsOpen(true);
    }
  };

  const closeReturnModal = () => {
    setModalIsOpen(false);
  };

  const closeModal = () => {
    dispatchGame({
      type: 'SET_ACTIVE_QUESTION_INDEX',
      payload: gameState.activeQuestionIndex + 1
    });
    setShowModal(false);
    setOpponentATimestamp(null);
    setTimestamp(null);
    setRecordMessage('');
    setOpponentAnsweredWrong(false);
    setYouAnsweredCorrect(false);
    setOpponentAnsweredCorrect(false);
    setYouAnsweredWrong(false);
    setYouAnsweredCorrect(false);
    setIsCorrect(false);
    setBattleFinishMessage("");
    if (gameState.activeQuestionIndex === (totalQuestions -1) && activeTestId !== null && !activeFinals && opponentA === "") {
      if (isAboveMaxScore && isValidScore && !isBelowEikenThreshold) {
        recordScore(gameState, activeTestId, setRecordMessage, setShowModal, setMaxScores, setError, setSignupModal);
      } else if (isBelowEikenThreshold) {
        setRecordMessage(`点数: ${yourScore}/${maximumScore}（７０％以下）記録されなかった！`);
        setShowModal(true);
      } else if (!isValidScore) {
        setRecordMessage(`点数: ${yourScore}/${maximumScore}（５０％以下）記録されなかった！`);
        setShowModal(true);
      } else {
        setRecordMessage(`今のスコア ${yourScore} は最高記録 ${activeTestMaxScore} を超えかった`);
        setShowModal(true);
      }
      setActiveTestId(null);
      dispatchGame({ type: 'RESET_GAME' });
    }
    if (gameState.activeQuestionIndex === (totalQuestions -1) && activeFinals) {
      recordFinalsScores(gameState, activeCategory, setRecordMessage, setShowModal, setError, setSignupModal)
      setActiveFinals(false);
      setActiveTestId(null);
      dispatchGame({ type: 'RESET_GAME' });
    }
  };

  useEffect(() => {
    if (gameState.activeQuestionIndex === (totalQuestions) && activeTestId !== null && opponentA !== "" && totalQuestions !== 0) {
      declareWinner(setShowModal, setBattleFinishMessage, setBattleModalPicture);
      setActiveTestId(null);
      dispatchGame({ type: 'RESET_GAME' });
    }
  }, [gameState.activeQuestionIndex, battleScore, opponentScore, totalQuestions]);


  const handleBackClick = () => {
    closeReturnModal();
    if (activeTestId !== undefined) {
      if (isAboveMaxScore && isValidScore && !isBelowEikenThreshold) {
        recordScore(gameState, activeTestId, setRecordMessage, setShowModal, setMaxScores, setError, setSignupModal);
      } else if (isBelowEikenThreshold) {
        setRecordMessage(`点数: ${yourScore}/${maximumScore}（７０％以下）記録されなかった！`);
        setShowModal(true);
      } else if (!isValidScore) {
        setRecordMessage(`点数: ${yourScore}/${maximumScore}（５０％以下）記録されなかった！`);
        setShowModal(true);
      } else {
        setRecordMessage(`今のスコア ${yourScore} は最高記録 ${activeTestMaxScore} を超えかった`);
        setShowModal(true);
      }
      toggleQuestionDetails({testId: activeTestId});
    } else if (gameState.activeQuestionIndex === (totalQuestions -1) && activeFinals) {
      setActiveFinals(false);
      setActiveTestId(null);
      recordFinalsScores(gameState, activeCategory, setRecordMessage, setShowModal, setError, setSignupModal);
    } else {
      setActiveFinals(false);
      setActiveTestId(null);
      setRecordMessage("まとめてストは最後までしないと記録されない");
      setShowModal(true);
    }
  };

  useEffect(() => {
    if (!loading && !error && activeTestId && randomizedValues && randomizedOptions && questions && testQuestions && inviter && ws && ws.readyState === WebSocket.OPEN) {
      sendData({
        type: "test_toggle",
        target_username: opponentA,
        test: activeTestId,
        randomizedValues: randomizedValues,
        randomizedOptions: randomizedOptions,
        questions: questions,
        testQuestions: testQuestions,
        totalQuestions: totalQuestions,
      });
    }
  }, [loading, error, activeTestId, randomizedValues, randomizedOptions, questions, testQuestions, inviter, ws, opponentA]);

  
  useEffect(() => {
    if (activeFinals) {
      toggleQuestionDetails({
        testId: undefined, testDescription: undefined, testDescriptionSound: undefined,
        numberOfQuestions: undefined, testName: undefined, category: activeCategory, maxPossibleScore: undefined,
      });      
    }
  }, [activeFinals]);

  useEffect(() => {
    setQuestionsDataCallback((data) => {
        setRandomizedValues(data.randomizedValues);
        setRandomizedOptions(data.randomizedOptions);
        setTestQuestions(prevQuestions => ({
          ...prevQuestions,
          questions: [...(prevQuestions.questions || []), ...data.testQuestions.questions],
        }));
        setQuestions(data.questions);
        setBattleScore(0);
        setOpponentScore(0);
    });
  }, [setRandomizedValues, randomizedOptions, setTestQuestions, setQuestions]);

  useEffect(() => {
    if (opponentA !== "") {
      setCategoryToggleCallback((category) => {
        console.log("Category toggled:", category);
        toggleCategories(category)
      });
    }
  }, [currentUser, opponentA, inviter, maxScores, activeCategory]);

  useEffect(() => {
    const handleTestToggle = async (data) => {
      setActiveTestId(data.test);
  
      setQuestions(data.questions);
      setTestQuestions(prevQuestions => ({
        ...prevQuestions,
        questions: [...(prevQuestions.questions || []), ...data.testQuestions.questions],
      }));
      setRandomizedValues(data.randomizedValues);
      setRandomizedOptions(data.randomizedOptions);
      setTotalQuestions(data.totalQuestions);
      setBattleScore(0);
      setOpponentScore(0);
      dispatchGame({
        type: 'SET_ACTIVE_QUESTION_INDEX',
        payload: 0,
      });
    };
    setTestToggleCallback(handleTestToggle);
  }, [currentUser]);

  const battleFeedbackWon = () => {
    setBattleModalPicture("https://storage.googleapis.com/ivar_reactions/openart-5eda95374c2140e3a6dad00334c41fef_raw%20(3).jpg");
    let audioUrl = "";

    if (currentCorrectAudioIndex === 1) {
      audioUrl = "https://storage.googleapis.com/ivar_reactions/2025_04_08_01_33_53_1.mp3";
    } else if (currentCorrectAudioIndex === 2) {
      audioUrl = "https://storage.googleapis.com/battle_mode/2025_04_20_23_44_59_1.mp3";
    } else if (currentCorrectAudioIndex === 3) {
      audioUrl = "https://storage.googleapis.com/battle_mode/2025_04_20_23_42_53_1.mp3";
    } else if (currentCorrectAudioIndex >= 4) {
      audioUrl = "https://storage.googleapis.com/battle_mode/2025_04_20_23_42_57_1.mp3";
    } else {
      audioUrl = "https://storage.googleapis.com/ivar_reactions/2025_04_08_01_33_53_1.mp3";
    }
  
    new Audio(audioUrl).play();
  };
  
  const battleFeedbackLost = () => {
    setBattleModalPicture("https://storage.googleapis.com/ivar_reactions/openart-12ba3e00450f41cc899c83c6a484c79f_raw%20(4).jpg");
    let audioUrl = "";

    if (currentWrongAudioIndex === 1) {
      audioUrl = "https://storage.googleapis.com/battle_mode/2025_04_20_23_42_05_1.mp3";
    } else if (currentWrongAudioIndex === 2) {
      audioUrl = "https://storage.googleapis.com/battle_mode/2025_04_20_23_42_10_1.mp3";
    } else if (currentWrongAudioIndex === 3) {
      audioUrl = "https://storage.googleapis.com/ivar_reactions/2025_04_08_00_19_52_1.mp3";
    } else if (currentWrongAudioIndex >= 4) {
      audioUrl = "https://storage.googleapis.com/battle_mode/2025_04_20_23_42_15_1.mp3";
    } else {
      audioUrl = "https://storage.googleapis.com/battle_mode/2025_04_20_23_42_05_1.mp3"; 
    }
  
    new Audio(audioUrl).play();
  };

  const battleFeedbackDraw = () => {
    setBattleModalPicture("https://storage.googleapis.com/battle_mode/ghibliibaru.jpg");
    new Audio("https://storage.googleapis.com/battle_mode/2025_04_21_00_30_26_1.mp3").play();
  };

  useEffect(() => {
    setCorrectWord(randomNumbers ? randomNumbers : randomWord);
    setCorrectSound(sound3 ? randomSound3 : sound2 ? randomSound2 : randomSound !== null ? randomSound : randomEikenUrl !== 't' ? randomEikenUrl : randomUrl);
    setCorrectPicture(randomPicture);
    setCorrectLabel(word2 ? randomWord2 : randomLabel);
    setCorrectEikenWord(randomCorrect);
    setCorrectAnswerKey(randomAlphabet);
  }, [currentUser, timestamp, opponentATimestamp, opponentAnsweredWrong, opponentAnsweredCorrect, youAnsweredCorrect, youAnsweredWrong]);

  useEffect(() => {
    if (((opponentAnsweredWrong && youAnsweredWrong) || (opponentAnsweredCorrect && !opponentAnsweredWrong)) && !showModal) {
      setIsCorrect(false);
      setCurrentCorrectAudioIndex(0);
      setCurrentWrongAudioIndex((prevIndex) => {
        const newIndex = (prevIndex + 1);
        return newIndex;
      });
      setCorrectOption(false);
      setSelectedOption(null);
      setShowModal(true);
    }
  }, [opponentAnsweredWrong, opponentAnsweredCorrect, youAnsweredCorrect, youAnsweredWrong]);


  useEffect(() => {
    setAnswerEchoSubmitCallback((data) => {
      setTimestamp(data.time_stamp);
      if (data.answered_wrong === true) {
        setYouAnsweredWrong(true);
      } else if (data.answered_wrong === false) {
        setYouAnsweredCorrect(true);
      }
    });
  }, [currentUser, timestamp, opponentATimestamp]);

  useEffect(() => {
    setAnswerSubmitCallback((data) => {
      setOpponentATimestamp(data.time_stamp);
      if (data.answered_wrong === false); {
        setOpponentAnsweredCorrect(true);
      }
      if (data.answered_wrong === true) {
        setOpponentAnsweredWrong(true);
      };
    });
  }, [currentUser, timestamp, opponentATimestamp]);

  useEffect(() => {
    if (testQuestions.questions.length > 0 && gameState.activeQuestionIndex !== undefined) {
      handleAutoPlay();
    }
  }, [testQuestions.questions, randomizedOptions, gameState.activeQuestionIndex]);

  useEffect(() => {
    if (showModal && opponentA !== "" && battleFinishMessage === "" && activeTestId !== null) {
      setCountdown(3); 

      const halfSecondTimer = setTimeout(() => {
        console.log("DEBUG -- timestamp:", timestamp, "opponentATimestamp:", opponentATimestamp);
        if ((opponentAnsweredCorrect === true && youAnsweredCorrect === false) || youAnsweredWrong || (youAnsweredCorrect && opponentAnsweredCorrect && opponentAnsweredWrong === false && timestamp > opponentATimestamp)) {
          console.log("DEBUG -- opponentAnsweredCorrect:", opponentAnsweredCorrect, "opponentAnsweredWrong:", opponentAnsweredWrong);
          if (opponentAnsweredCorrect === true && opponentAnsweredWrong === false){
            setOpponentScore((prev) => prev + 1);
          }
          battleFeedbackLost();
        } else if (youAnsweredCorrect && opponentAnsweredCorrect && opponentAnsweredWrong === false && timestamp === opponentATimestamp) {
          battleFeedbackDraw();
        } else {
          setBattleScore((prev) => prev + 1);
          battleFeedbackWon();
          console.log("BS:", battleScore);
          console.log("OS:", opponentScore);
        }
      }, 500);
      const interval = setInterval(() => {
        setCountdown((prev) => prev - 1);
      }, 1000);
  
      const timer = setTimeout(() => {
        if (gameState.activeQuestionIndex === (totalQuestions - 1) && opponentA !== "" && totalQuestions !== 0) {
          declareWinner(setShowModal, setBattleFinishMessage, setBattleModalPicture);   
        }
        setOpponentAnsweredCorrect(false);
        setOpponentAnsweredWrong(false);
        setYouAnsweredCorrect(false);
        setYouAnsweredWrong(false);
        closeModal();
      }, 3000);
  
      return () => {
        clearTimeout(timer);
        clearInterval(interval);
        clearTimeout(halfSecondTimer);
      };
    }
  }, [showModal, timestamp, opponentATimestamp, youAnsweredCorrect, youAnsweredWrong, opponentAnsweredWrong, opponentAnsweredCorrect, dispatchGame.activeQuestionIndex]);

  const toggleIbaruCharacters = () => {
    setActiveIbaruCharacters(!activeIbaruCharacters);
    setActivity(!activeIbaruCharacters ? "ibaru" : "");
  };

  const excludedFinalsCategories = ['jr_1', 'jr_2', 'jr_3', 'eiken_pre2', 'eiken2'];

  return (
    <div>
      <div className="flex-center-column">
        <div>
        <div className="quiz-header flex-center-column">
          <>
          {!currentUser &&
            <Login />
          }
          {!activeCategory && activity !== "memories" &&
          <IbaruCharactersButton toggleIbaruCharacters={toggleIbaruCharacters} activeIbaruCharacters={activeIbaruCharacters} />
          }
          {activeIbaruCharacters &&
          <IbaruCharacters isEnglish={isEnglish} />
          }
          {!activeIbaruCharacters && activity !== "memories" && (opponentA === "" || inviter) &&
          <div ref={testSectionRef}>
          <CategoryButtons isEnglish={isEnglish} toggleCategories={toggleCategories} activeCategory={activeCategory} currentUser={currentUser} />
          {!activeTestId && !activeMusicVideos && !activeFinals &&
            <CategoryReturnButton isEnglish={isEnglish} toggleCategories={toggleCategories} activeCategory={activeCategory} activeTestId={activeTestId} />
          }
          </div>
          }
          {urlPath === "/portfolio/" && !activeCategory &&
              <div>
                {isEnglish
                  ? 'Here we have the category buttons. Click on them, choose a test and then play'
                  : 'ここにカテゴリーボタンがあります。クリックでテストを選び、プレイしてみてください'
                }
              </div>
          }
          {(isEikenOrPhonics && !activeFinals) ? (
            <h4>
              {isEnglish
                ? "Your score won't be recorded unless you get at least 70%."
                : "７割以上とらないと点数は記録されない"}
            </h4>
          ) : ''}

          {(!isEikenOrPhonics && !activeFinals && activeCategory !== null) ? (
            <h4>
              {isEnglish
                ? "Your score won't be recorded unless you get at least 50%."
                : "５割以上とらないと点数は記録されない"}
            </h4>
          ) : ''}
          
          {activeFinals && (
            <h4>
              {isEnglish
                ? "You must finish the test and score at least 80% for your score to be recorded."
                : "最後までやるのと、８割以上とらないと点数は記録されない"}
            </h4>
          )}
          </>
            {loading && <p>Loading...</p>}
            {error && <p>{error}</p>}
                <div className="test-buttons-container" style={{ display: !activeTestId ? 'flex' : undefined, flexWrap: !activeTestId ? 'wrap' : undefined }}>
                {inviter &&
                  <div>自分と{opponentA}がどちらも点数７割以上とっているテスト以外はバトルで使えない！</div>
                }
                {activeCategory && currentUser?.teacher && !activeFinals && activeTestId === null && (opponentA === "") &&
                  <MusicVideoButton setActiveMusicVideos={setActiveMusicVideos} activeMusicVideos={activeMusicVideos} />
                }
                {activeCategory && !excludedFinalsCategories.includes(activeCategory) && !activeMusicVideos && !activeFinals && activeTestId === null && (opponentA === "") &&
                  <FinalsButton setActiveFinals={setActiveFinals} />
                }
                {activeFinals && (opponentA === "" || inviter) &&
                  <FinalsReturnButton openModal={openModal} gameState={gameState} isEnglish={isEnglish} audioRef={audioRef} />
                }
                {(opponentA === "" || inviter) && !activeMusicVideos && filteredTests.map(test => (
                    <span key={test.id}>
                    {activeTestId !== null ? (
                        <TestReturnButton openModal={openModal} gameState={gameState} isEnglish={isEnglish} activeTestId={activeTestId} test={test} audioRef={audioRef} urlPath={urlPath} />
                    ) : (
                      <TestButtons test={test} activeCategory={activeCategory} activeTestId={activeTestId} toggleQuestionDetails={toggleQuestionDetails} maxScores={maxScores} isEnglish={isEnglish} audioRef={audioRef} urlPath={urlPath} />
                    )}
                    </span>
                ))}
                {activeMusicVideos && activeCategory === "numbers" && (
                  <div className="d-flex flex-wrap gap-3 my-3 justify-content-center">
                    <iframe
                      width="480"
                      height="280"
                      src="https://www.youtube.com/embed/e0dJWfQHF8Y?si=AM1T2_MK86lVHdHH"
                      title="YouTube video player"
                      frameborder="0"
                      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                      referrerpolicy="strict-origin-when-cross-origin"
                      allowfullscreen="true"
                    ></iframe>
                    <iframe
                      title="Counting 1‑20 – Super Simple Songs"
                      width="480"
                      height="280"
                      src="https://www.youtube.com/embed/mXMofxtDPUQ"
                      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                      allowFullScreen
                    />
                    <iframe
                      title="Count 1‑100 – KidsTV123"
                      width="480"
                      height="280"
                      src="https://www.youtube.com/embed/EUu96La3OlM"
                      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                      allowFullScreen
                    />
                  </div>
                )}
                {activeMusicVideos && activeCategory === "phonics" && (
                  <div className="d-flex flex-wrap gap-3 my-3 justify-content-center">
                    <iframe
                      width="360"
                      height="210"
                      src="https://www.youtube.com/embed/75p-N9YKqNo"
                      title="ABC Song – Kidsongs"
                      frameBorder="0"
                      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share; fullscreen"
                      referrerPolicy="strict-origin-when-cross-origin"
                      allowFullScreen
                    />
                    <iframe
                      width="360"
                      height="210"
                      src="https://www.youtube.com/embed/w_-lz2BI2Co?si=16QU0oVjBxjEDaS2" 
                      title="YouTube video player"
                      frameborder="0"
                      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                      referrerpolicy="strict-origin-when-cross-origin"
                      allowfullscreen="true"
                    ></iframe>
                    <iframe
                      width="360"
                      height="210"
                      src="https://www.youtube.com/embed/BELlZKpi1Zs?si=8dvXi7hTvET4s8CW" 
                      title="YouTube video player"
                      frameborder="0"
                      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                      referrerpolicy="strict-origin-when-cross-origin"
                      allowfullscreen="true"
                    ></iframe>
                    <iframe
                      width="360"
                      height="210"
                      src="https://www.youtube.com/embed/ffeZXPtTGC4?si=ntFk92UFAwXg_hJH" 
                      title="YouTube video player"
                      frameborder="0"
                      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                      referrerpolicy="strict-origin-when-cross-origin"
                      allowfullscreen="true"
                    ></iframe>
                  </div>
                )}
                {activeTestId !== null && (
                  <>
                  <div className="test-details flex-center-column" >
                  {!isRealStoryTest && isPractice && questions && ((shouldShowEikenPracticeSection) || activeCategory !== 'eiken') && (
                    <Practice questions={questions} handlePlay={handlePlay} isPlayDisabled={isPlayDisabled} activeTestDescription={activeTestDescription} activeTestDescriptionSound={activeTestDescriptionSound} />
                  )}
                  {!isPractice && questions && (questions.sound3 || questions.display_all) && (
                    <div key={questions.id} style={{ display: 'flex', flexWrap: 'wrap' }}>
                      <DisplayAllVocab questions={questions} shuffledKeys={gameState.shuffledKeys} />
                    </div>
                  )}
                    <ul>
                      {!isPractice && currentQuestion?.story_picture_url &&
                      <div>
                        <img src={currentQuestion?.story_picture_url} alt="Question" width="700" height="500" />
                      </div>
                      }
                      {!isPractice && currentQuestion?.story &&
                            <div
                              style={{
                                height: '400px',            
                                width: '100%',              
                                maxWidth: '700px',         
                                overflowY: 'scroll',        
                                padding: '1rem 1.5rem',    
                                borderRadius: '12px',      
                                border: '2px solid #888',   
                                boxShadow: '0 4px 12px rgba(0,0,0,0.5)',  
                                backgroundColor: '#000',  
                                fontFamily: '"Georgia", serif',  
                                fontSize: '16px',
                                lineHeight: '1.6',
                                color: '#fff',  
                                boxSizing: 'border-box',
                                marginBottom: '20px',
                              }}
                            >
                            {(((currentMaxScore &&  currentMaxScore.score === currentMaxScore.total_questions) || currentUser?.teacher) ? currentQuestion.full_story : currentQuestion.story).split('\n').map((line, index) => (
                                <React.Fragment key={index}>
                                  {line}<br />
                                </React.Fragment>
                            ))}
                            </div>
                      }
                      {!isPractice && currentQuestion?.full_story_sound_url && (scoreRatio >= 0.8 || currentUser?.teacher) && (
                        <div style={{ marginTop: "10px" }}>
                          <audio
                            controls
                            src={currentQuestion.full_story_sound_url}
                            style={{ width: "100%", maxWidth: "700px" }}
                          >
                            Your browser does not support the audio element.
                          </audio>
                        </div>
                      )}


                      {!isPractice && testQuestions.questions
                      .filter(question => question.test === activeTestId && !question.category_on || (activeFinals && question.category_on === activeCategory))
                      .sort((a, b) => (b.sound3 ? 1 : 0) - (a.sound3 ? 1 : 0))
                      .map((question, index) => {
                        let selectedKeys = [];
                        const keys = Object.keys(question.question_list);
                        const shuffledKeys = shuffleArray([...keys]);
                        const shuffledValues = shuffledKeys.map((key) => question.question_list[key]);
                        
                        return (
                          <>
                          <div key={question.id} className={index === gameState.activeQuestionIndex ? 'active' : 'd-none'}>
                            {!sound3 ? (
                              isPicture ? (
                                <img src={randomUrl} alt="Question" width="200" height="150" />
                              ) : question.label ? (
                                <>
                                  <img src={randomPicture} alt="Question" width="200" height="150" />
                                  <h5>{randomWord}</h5>
                                </>
                              ) : randomSound ? null : null
                            ) : null}
                            {!isEikenAudio && !randomWrongThree && !question.name.includes("ヘボン式") &&
                            <button className="play_buttons btn btn-success mb-3" style={{ border: '5px solid black' }} onClick={(e) => handleAutoPlay()} disabled={isPlayDisabled}>
                                  {isEnglish ? "Play sound" : "音声"} <FaPlay style={{ marginLeft: '10px' }} />
                            </button>    
                            }


                            <div>
                            {randomCorrect ? (
                              <h4
                                style={{ whiteSpace: 'pre-wrap' }}
                                dangerouslySetInnerHTML={{ __html: formatTextAsHtml(randomAlphabet) }}
                              />
                            ) : randomTranslation ? (
                                <h4 style={{ whiteSpace: 'pre-wrap' }} 
                                      dangerouslySetInnerHTML={{ 
                                        __html: `${question.story ? '(　' : ''}${randomTranslation}${question.story ? '　)　のなかに入る言葉を選んでください' : ''}` 
                                      }} 
                                />
                            ) : (!isAudio && typeof randomUrl === 'string' && randomUrl.trim() !== '') ? (
                              <h4>
                                （{randomUrl}）
                              </h4>
                            ) : null}
                            {randomNumbers && (
                            <h4 style={{ whiteSpace: 'pre-wrap' }}>{wrapTextSafe(randomWord, 50).join('\n')}</h4>
                            )}
                            {question.japanese_option && (
                              <h4 style={{ whiteSpace: 'pre-wrap' }}>{wrapTextSafe(randomWord, 50).join('\n')}</h4>
                            )}
                            {question.description && !question.write_answer && (
                              <h4 style={{ whiteSpace: 'pre-wrap' }}>{wrapTextSafe(question.name, 50).join('\n')}</h4>
                            )}
                            {!youAnsweredWrong ? (
                            <TestRenderForm 
                              {...{ 
                                timestamp, question, selectedKeys, randomizedOptions, correctOption, opponentAnsweredWrong, selectedOption, inputValue, 
                                setInputValue, setCorrectOption, setSelectedOption, handleSubmit, randomAlphabet, randomLabel, randomWord2, 
                                randomPicture, randomCorrect, randomWord, randomJapanese, randomNumbers, questions, randomEikenUrl, randomWrongThree, randomFifth,
                                randomWrongOne, randomWrongTwo, randomAlphabetSliced, opponentA, ws, isEnglish, gameState, sendData, handlePlay, 
                                isPractice, activeCategory, youAnsweredWrong, setYouAnsweredWrong}} 
                            />
                            ) : (
                            <div className="text-danger text-center fs-4 mt-3">
                              君が間違えた！相手が答えるまで待つしかない！
                            </div>
                            )
                            }
                            </div>
                          </div>
                          </>
                        );
                      })}
                    </ul>
                  </div>
                  </>
                )}
                </div>
        </div>
          {activeTestId !== null &&
            <>
            <h1 translate="no">{opponentA === "" ? "質問ナンバー" : "ラウンド"}{gameState.activeQuestionIndex + 1}/{totalQuestions}</h1>
            <h1 translate="no">満点＝{maximumScore}</h1>
            </>
          }
            <div>
            {opponentA !== '' &&
              <h2>{opponentA}{activeTestId !== null ? "と命がけの戦いをしている" : "とバトル準備ができている"}</h2>
            }
            </div>
        </div>
    </div>
    <TestModal
      showModal={showModal} closeModal={closeModal} currentCorrectAudioIndex={currentCorrectAudioIndex} recordMessage={recordMessage} isCorrect={isCorrect}
      correctSound={correctSound} handlePlay={handlePlay} isPlayDisabled={isPlayDisabled} isEnglish={isEnglish} correctLabel={correctLabel}
      correctWord={correctWord} correctEikenWord={correctEikenWord} correctAnswerKey={correctAnswerKey} currentWrongAudioIndex={currentWrongAudioIndex} gameState={gameState} opponentA={opponentA} countdown={countdown}
      battleModalPicture={battleModalPicture} battleModalText={battleModalText} battleFinishMessage={battleFinishMessage} battleScore={battleScore} opponentScore={opponentScore} timestamp={timestamp} opponentATimestamp={opponentATimestamp} scoreMultiplier={scoreMultiplier} activeTestMaxScore={activeTestMaxScore} activeFinals={activeFinals}
    />
    <ReturnConfirmModal modalIsOpen={modalIsOpen} closeReturnModal={closeReturnModal} handleBackClick={handleBackClick} />
    <SignupPromptModal signupModal={signupModal} closeSignupModal={closeSignupModal} />
    <InvitationModal />
    </div>
  );
};
export default Test;