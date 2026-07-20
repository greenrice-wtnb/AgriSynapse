# AgriSynapse
Open-source industrial IoT system featuring autonomous sensor-to-actuator control via private LoRa, with real-time edge node monitoring through GAS and a web application.

# AgriSynapse

**AgriSynapse** は、LoRa通信とクラウド（Googleスプレッドシート / GAS / Discord）を組み合わせた**環境センシングおよび自動管理・負荷制御システム**です。
現地に行かなくても、スマートフォンやPCからセンサーによる環境監視、負荷（アクチュエーター）の遠隔操作、システムの稼働状況の確認が行えます。

## 🌟 特徴 (Features)

*   **📡 長距離・省電力通信 (LoRa)**
    携帯電話の電波が届かない圃場でも、見通しの良いゲートウェイやリピーターを介して長距離通信が可能です。センサーノードは単1乾電池3本で長期間駆動します。
*   **☁️ クラウド連携と完全自動化**
    GoogleスプレッドシートとGoogle Apps Script (GAS) をデータベース・バックエンドとして活用。指定した閾値に基づき、システムが自律的に接続されたアクチュエーター（電子機器）を制御して環境を制御します。
*   **📱 スマホから簡単監視・操作**
    専用のWebダッシュボードから、リアルタイムの状態、バッテリー電圧、通信状態を確認可能。また、手動でのアクチュエーター制御もスマホからワンタップで実行できます。
*   **🔔 Discord通知機能**
    閾値超過の通知や、電圧低下、通信途絶、アクチュエーターの過負荷（エラー）などをDiscordへ自動通知。異常の早期発見に貢献します。
*   **🔒 安全で混信のない通信**
    システム共通の認証キー（AUTH_KEY）と各ノード固有のIDを使用することで、近隣で同システムが稼働していても混信・誤作動を防ぎます。

## 🏗 システム構成 (Architecture)

AgriSynapseは、以下の複数のハードウェアノードとクラウドシステムで構成されています。

| ノード名 | 役割・特徴 | 電源 |
| :--- | :--- | :--- |
| **センサーノード** | 圃場の環境を定期計測し、ゲートウェイへの報告とアクチュエーターノードへの操作指示を行う「司令塔」。 | 単1乾電池 × 3 |
| **アクチュエーターノード** | センサーからの指示を受け、モーターやリニアアクチュエーターを駆動して水門を開閉。過負荷検知機能付き。 | 12V 鉛蓄電池 |
| **ゲートウェイ (LTE-M / Wi-Fi)** | 各圃場からLoRaで集めたデータをインターネット経由でクラウドへ転送するハブ。 | 5V USB (Wi-Fi) / 12V (LTE-M) |
| **LoRaリピーター (中継機)** | ゲートウェイと圃場間の電波が届きにくい場合の中継役。異チャンネル中継対応。 | 12V 鉛蓄電池 + ソーラー |
| **スマートコントローラー** | 現地での設定変更（閾値、通信CH）や、デバッグの補助、アクチュエーターノードのテスト操作を無線(LoRa)で行うための携帯型端末。 | モバイルバッテリー等 (5V) |

## 🚀 始め方 (Getting Started)

詳細なセットアップ手順や配線図、部品リストについては、[AgriSynapseの紹介ページ](https://greenrice-wtnb-farm.jimdofree.com/agrisynapse/?preview_sid=358720)または同梱の `📘 水田水位自動管理システム 統合取扱説明書.docx` をご参照ください。

### 大まかな導入ステップ
1. **クラウドの準備**
   Googleスプレッドシートを作成し、付属のGASスクリプト(`管理用GASスクリプト...txt`)をデプロイします。
   管理用GASスクリプト内のMyFunction()関数を、GoogleAppsScriptのエディター内から指定して実行することで、動作に必要な全シートの作成が自動で行われます。
   また、Cloudflare PagesでWebアプリ用HTMLを設定・デプロイします。Cloudflare Workersを用いたリダイレクト追従プロキシも設定します。
3. **ゲートウェイのセットアップ**
   Raspberry Pi Pico(LTE-M版) またはPico W(Wi‐Fi版)にコードを書き込み、LTE-MまたはWi‐Fi経由でGASと通信できるように設定します。
4. **ノードの組み立てと初期設定**
   センサーノード、アクチュエーターノードを組み立てます。電源投入時、3分間APモードで起動するため、スマホから `192.168.4.1` にアクセスし、
   ノードIDやLoRaチャンネル、TDMA用オフセット値などを設定します。
6. **現地設置とキャリブレーション**
   現地圃場に設置後、スマートコントローラーを使用して実際の環境でセンサーのキャリブレーション（基準値の設定）を行います。

### 補足事項
*  **センサーノード**は、複数種類のセンサーを同時に制御・計測できますが、ゲートウェイに送信できる計測データは、デフォルトでは2種類です。
   * デフォルトの`センサーノード用コード.py`では、10分毎にセンサー計測を行い、DATAパケットで、超音波測距センサーで計測した水位(water_level)と、
   自身に保存された閾値とそれを照らし合わせたパーセンテージ(level_pct)を、30分毎にゲートウェイに向けて送信していますが、この2種類の変数に、それぞれ別々のセンサーから取得した値を割り当てることが可能です。
   * ゲートウェイ・GAS共に、センサーノードから送られてきた数値をただ受け取って、適切に格納・記録しているだけで、**それが何の数値なのか**を判別する機能はありません。よって、GASにおける各数値の単位や通知内容、閾値の設定を任意に変更しても、不用意にコードを改変しない限りは、各ノード間の通信における数値の取り扱いに影響はありません。
   * センサーノードに搭載するセンサーの種類を増設し、送信するデータの種類や閾値判定機能、それに合わせた通知機能を増やしたい場合は、センサーノードが送信する`DATA`パケットの構造や、ゲートウェイノードがそれを受けて管理用GASにリレーする際のJSONデータの構造、および、管理用GASが受け取ったデータを対応する各セルに正しく格納できるようすること、管理用ダッシュボードの各カードに正しく各データが表示されるようにすること等に配慮してコードの改変を行ってください。特に、管理用GASの後方互換性の維持に努めてください。

*  **アクチュエーターノード**について、`Type-R-O`と`Type-R-P`の2種類の動作用コードを設定しています。
   末尾が「‐O」のPythonコードは、ON制御版のコードであり、「‐P」のコードはパルス制御版のコードとなっています。
   デフォルトの動作としては、
   * `Type-R-O` では、`OPEN`指示の`COMMAND`パケットを受信した場合、次回以降の`COMMAND`パケットで`CLOSE`指示を受信するまでの間、リレーの状態を保持します。
   逆の場合も同様です.
   * `Type-R-P` では、`COMMAND`パケットで`OPEM`または`CLOSE`の指示を受信した場合、それぞれの指示に対して、設定されたリレーのチャンネルを20秒間ONにし、
   その後OFFにします。

*  **稼働モード**について
   センサーノードとアクチュエーターノードは、GASにデプロイした`管理用GASスクリプト`や、Cloudflare Pagesにデプロイした`dashboard`上から、
   稼働モードを`RUN`または`STOP`に設定することが可能です。
   
   * **センサーノード**を`STOP`モードに設定した場合
     システムの中核となる通信制御が停止します。
     * 閾値の判定機能および、アクチュエーターノードへの`COMMAND`パケット送信が停止します。
     * 閾値判定の権限が管理用GASに委譲され、閾値を上下に抜けた場合はDiscordでその旨が管理者に通知されます。
     * 10分毎のセンサー計測、および、30分毎のゲートウェイへの`DATA`パケット送信は、継続して実施されます。
       
   * **アクチュエーターノード**を`STOP`モードに設定した場合
     **アクチュエーターのメンテナンス時**に、予期せぬ自動作動で指を挟むなどの事故を防ぐための**安全ロック**として使用します。
     * センサーノードからの`COMMAND`パケットによる操作指示を拒否します。
     * スマートコントローラーからの`MANUAL`コマンドによる操作のみ、メンテナンス時のテストコマンドとして扱い、受信して動作します。
   * **リピーターノード**を`STOP`モードに設定した場合
　　　リピーターノードのSTOPモードは、対象の圃場がシーズンオフに入り、電波の中継が不要になった際に、電波空間の混雑（不要な通信）を減らすために使用します。
     * 中継機能の停止: 子機（センサーノード・アクチュエーターノード）からデータを受信しても、親機（ゲートウェイ）へのパケット転送（リピート）を一切行わなくなります。
     * 死活監視のみ継続: 中継機能は停止しますが、リピーター自身が生きているか（バッテリー電圧など）をゲートウェイに送信する「自己レポート」の通信のみ、
     設定されたオフセット時間に従って送信し続けます。
  * **ゲートウェイノード**には、稼働モードの変更機能はありません。
  * 管理用ダッシュボード( `dashboard` )のヘッダーにある稼働モードボタンをクリックすることで、システムを設置した全圃場にあるセンサーノードの稼働モードを、一括で変更することが可能です。また、管理用ダッシュボードの各圃場IDのカード上部にある稼働モード変更ボタンで、圃場別にセンサーノードの稼働モードを変更することが可能です。

## ⚠️ 免責事項 (Disclaimer)

本システムはオープンソースとして無償で公開されています。
配線ミスやコードの改変、接続した負荷（ポンプやモーター）の不具合に伴う機器の故障、センサー不具合、自然災害等のいかなる損害についても、開発者および貢献者は一切の責任を負いません。事前に十分にテストを行い、自己責任の範囲内で安全に配慮して運用してください。

## 📄 ライセンス (License)

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.

---

# AgriSynapse (English)

**AgriSynapse** is an **environmental sensing, automated management, and load control system** combining LoRa communication and cloud services (Google Sheets / GAS / Discord). 
It allows you to monitor environmental conditions via sensors, remotely control loads (actuators), and check system operational status from your smartphone or PC without having to visit the site.

## 🌟 Features

*   **📡 Long-range, Low-power Communication (LoRa)**
    Capable of long-distance communication via gateways or repeaters, even in fields without cellular network coverage. Sensor nodes can operate for an extended period on just three D-cell batteries.
*   **☁️ Cloud Integration & Full Automation**
    Utilizes Google Sheets and Google Apps Script (GAS) as a database and backend. Based on specified thresholds, the system autonomously controls connected actuators (electronic devices) to manage the environment.
*   **📱 Easy Monitoring & Control from Smartphone**
    Check real-time status, battery voltage, and communication status from a dedicated web dashboard. Manual actuator control can also be executed with a single tap from your smartphone.
*   **🔔 Discord Notifications**
    Automatically sends notifications to Discord regarding threshold breaches, voltage drops, communication loss, and actuator overloads (errors), contributing to early anomaly detection.
*   **🔒 Secure & Interference-free Communication**
    Uses a system-wide authentication key (AUTH_KEY) and unique IDs for each node to prevent signal interference and malfunctions, even if similar systems are operating nearby.

## 🏗 Architecture

AgriSynapse consists of the following hardware nodes and cloud components:

| Node Name | Role & Features | Power Source |
| :--- | :--- | :--- |
| **Sensor Node** | The "Command Center" that periodically measures field environments, reports to the gateway, and sends operation instructions to the actuator node. | 3x D-cell batteries |
| **Actuator Node** | Receives instructions from the sensor and drives motors or linear actuators to open/close water gates. Includes overload detection. | 12V Lead-acid battery |
| **Gateway (LTE-M / Wi-Fi)** | A hub that transfers data collected from each field via LoRa to the cloud over the internet. | 5V USB (Wi-Fi) / 12V (LTE-M) |
| **LoRa Repeater** | Relays signals when radio waves struggle to reach between the gateway and the fields. Supports cross-channel relaying. | 12V Lead-acid battery + Solar panel |
| **Smart Controller** | A portable terminal for changing settings on-site (thresholds, communication CH), assisting with debugging, and performing test operations of actuator nodes via wireless LoRa. | Mobile battery, etc. (5V) |

## 🚀 Getting Started

For detailed setup instructions, wiring diagrams, and parts lists, please refer to the [AgriSynapse WebSite (in Japanese)](https://greenrice-wtnb-farm.jimdofree.com/agrisynapse/?preview_sid=358720) or included `📘 水田水位自動管理システム 統合取扱説明書.docx` (Comprehensive Instruction Manual - currently in Japanese).

### Basic Setup Steps
1. **Cloud Preparation**
   Create a Google Sheet and deploy the provided GAS scripts (`管理用GASスクリプト...txt`).
   By selecting and running the MyFunction() function from within the Google Apps Script editor, all the sheets necessary for operation will be created automatically.
   Also, configure and deploy the Web App HTML via Cloudflare Pages. Set up a redirect-following proxy using Cloudflare Workers.
3. **Gateway Setup**
   Flash the code to a Raspberry Pi Pico (LTE-M version) or Pico W (Wi-Fi version) and configure it to communicate with GAS via LTE-M or Wi-Fi.
4. **Node Assembly & Initial Setup**
   Assemble the sensor and actuator nodes. Upon power-up, they boot in AP mode. Access `192.168.4.1` from a smartphone to set node IDs, LoRa channels, TDMA offset values, etc.
5. **On-site Installation & Calibration**
   After installing the system in the field, use the smart controller in the actual environment to calibrate the sensors (setting the reference values).

### Supplementary Notes
*  The **Sensor Node** can control and measure multiple types of sensors simultaneously, but by default, it can only send two types of measurement data to the gateway.
   * In the default `センサーノード用コード.py`, sensor measurements are taken every 10 minutes, and the water level measured by the ultrasonic distance sensor (water_level) and the percentage calculated against its internally saved threshold (level_pct) are sent to the gateway every 30 minutes via a `DATA` packet. It is possible to assign values obtained from different sensors to these two types of variables.
   * Both the gateway and GAS simply receive the numerical values sent from the sensor node and appropriately store and record them; they do not have a function to determine **what the values represent**. Therefore, even if you arbitrarily change the units of each value, notification contents, or threshold settings in GAS, it will not affect the handling of numerical values in the communication between nodes, as long as the code is not carelessly modified.
   * If you wish to add more types of sensors to the sensor node, increase the types of data sent, the threshold judgment functions, and their corresponding notification functions, please modify the code with consideration for the structure of the `DATA` packet sent by the sensor node, the structure of the JSON data when the gateway node receives and relays it to the management GAS, ensuring that the management GAS can correctly store the received data in the corresponding cells, and ensuring that each data is correctly displayed on each card of the management dashboard. In particular, please strive to maintain backward compatibility with the management GAS.

*  Regarding the **Actuator Node**, two types of operational codes, `Type-R-O` and `Type-R-P`, are provided.
   The Python code ending with "-O" is the ON-control version code, and the code ending with "-P" is the pulse-control version code.
   As for the default behavior:
   * In `Type-R-O`, when a `COMMAND` packet with an `OPEN` instruction is received, it retains the relay state until a `CLOSE` instruction is received in a subsequent `COMMAND` packet.
   The reverse case is also the same.
   * In `Type-R-P`, when an `OPEM` or `CLOSE` instruction is received via a `COMMAND` packet, the configured relay channel is turned ON for 20 seconds for each respective instruction,
   and then turned OFF.

*  Regarding **Operation Mode**
   The sensor node and actuator node can have their operation mode set to `RUN` or `STOP` from the `管理用GASスクリプト` deployed on GAS or the `dashboard` deployed on Cloudflare Pages.
   
   * When the **Sensor Node** is set to `STOP` mode
     The communication control, which is the core of the system, stops.
     * The threshold judgment function and the transmission of `COMMAND` packets to the actuator node will stop.
     * The authority for threshold judgment is delegated to the management GAS, and if the threshold is exceeded upwards or downwards, the administrator is notified via Discord.
     * Sensor measurements every 10 minutes and the transmission of `DATA` packets to the gateway every 30 minutes will continue to be executed.
       
   * When the **Actuator Node** is set to `STOP` mode
     This is used as a **safety lock** during **actuator maintenance** to prevent accidents such as pinched fingers due to unexpected automatic operation.
     * It rejects operation instructions via `COMMAND` packets from the sensor node.
     * Only operations via the `MANUAL` command from the smart controller are treated as test commands during maintenance, and are received and executed.
   * When the **Repeater Node** is set to `STOP` mode
     The STOP mode of the repeater node is used to reduce congestion in the radio wave space (unnecessary communication) when the target field enters the off-season and radio wave relaying is no longer necessary.
     * Suspension of relay function: Even if data is received from child devices (sensor node/actuator node), packet forwarding (repeating) to the parent device (gateway) will completely stop.
     * Continuation of keep-alive monitoring only: Although the relay function stops, only the "self-report" communication, which sends information on whether the repeater itself is alive (battery voltage, etc.) to the gateway,
     will continue to be sent according to the set offset time.
  * The **Gateway Node** does not have an operation mode change function.
  * By clicking the operation mode button in the header of the management dashboard ( `dashboard` ), you can batch change the operation modes of the sensor nodes in all fields where the system is installed. Additionally, you can change the operation mode of the sensor node on a field-by-field basis using the operation mode change button at the top of each field ID card on the management dashboard.

## ⚠️ Disclaimer

This system is published free of charge as open source. The developers and contributors assume no responsibility for any damages, including equipment failure resulting from wiring mistakes, code modifications, or malfunctions of connected loads (pumps/motors), sensor defects, or natural disasters. Please test thoroughly and operate safely at your own risk.

## 📄 License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.
